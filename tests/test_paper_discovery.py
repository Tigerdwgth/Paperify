"""``src.paper_discovery`` 单元测试。

全部 mock 外部 IO（HF API / arxiv API / opencode subprocess / 文件读写），
不依赖网络也不污染真实 cache/published_papers.json。
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _make_candidate(arxiv_id="2410.11758", title="Embodied VLA Robot",
                    abstract="A robot with imitation learning",
                    submitted_date="2026-04-22", upvotes=5,
                    github_repo="https://github.com/foo/bar",
                    project_page=None, source="hf"):
    from src.paper_discovery import Candidate
    return Candidate(
        arxiv_id=arxiv_id, title=title, abstract=abstract,
        url="https://arxiv.org/abs/%s" % arxiv_id,
        submitted_date=submitted_date, upvotes=upvotes,
        github_repo=github_repo, project_page=project_page, source=source,
    )


# ---------------------------------------------------------------------
# 1. profile 加载
# ---------------------------------------------------------------------

def test_load_profile_default():
    """读 config/discovery_profile.yaml 应返回含 topics/weights 的 dict。"""
    from src.paper_discovery import _load_profile, _default_profile_path
    profile = _load_profile(_default_profile_path())
    assert isinstance(profile, dict)
    assert "topics" in profile and isinstance(profile["topics"], list)
    assert "weights" in profile and isinstance(profile["weights"], dict)
    assert "min_score" in profile
    # 默认值没被吞
    assert profile["weights"].get("topic_match", 0) >= 1


# ---------------------------------------------------------------------
# 2. HF 数据源
# ---------------------------------------------------------------------

def test_gather_hf_mock():
    """mock requests.get 返回 fixture HF JSON → 至少 2 条 Candidate, 含 github_repo。"""
    fixture = [
        {
            "paper": {
                "id": "2604.20156",
                "title": "Foo VLA Robot Manipulation",
                "summary": "A great paper",
                "upvotes": 10,
                "githubRepo": "https://github.com/foo/foo",
                "projectPage": "https://foo.example.com",
                "publishedAt": "2026-04-22T00:00:00Z",
            },
            "publishedAt": "2026-04-22T00:00:00Z",
            "title": "Foo VLA Robot Manipulation",
            "summary": "A great paper",
        },
        {
            "paper": {
                "id": "2604.99999",
                "title": "Bar Imitation Learning",
                "summary": "Another paper",
                "upvotes": 3,
                "githubRepo": None,
                "projectPage": None,
                "publishedAt": "2026-04-23T00:00:00Z",
            },
        },
    ]
    fake_resp = MagicMock()
    fake_resp.raise_for_status.return_value = None
    fake_resp.json.return_value = fixture
    with patch("src.discovery_sources.hf_papers.requests.get",
               return_value=fake_resp) as m:
        from src.discovery_sources import fetch_hf_daily
        cands = fetch_hf_daily(limit=10)
    assert m.called
    assert len(cands) == 2
    assert cands[0].arxiv_id == "2604.20156"
    assert cands[0].github_repo == "https://github.com/foo/foo"
    assert cands[0].project_page == "https://foo.example.com"
    assert cands[0].source == "hf"
    assert cands[1].github_repo is None


# ---------------------------------------------------------------------
# 3. arxiv 数据源
# ---------------------------------------------------------------------

def _fake_arxiv_paper(link: str, days_ago: int, title: str = "Embodied AI Robot"):
    """构造一篇 mock arxiv 论文; 日期相对今天, 否则会被 days 窗口过滤掉。"""
    import datetime
    from src.get_arxiv_latest import Paper
    ts = datetime.datetime.now() - datetime.timedelta(days=days_ago)
    stamp = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return Paper(title=title, authors=["A", "B"], abstract="VLA paper",
                 link=link, announced_date=stamp, submitted_date=stamp,
                 comments=""), ts.strftime("%Y-%m-%d")


def test_gather_arxiv_mock():
    """mock get_latest_embodied_ai_papers → Candidate 列表。"""
    p1, day1 = _fake_arxiv_paper("https://arxiv.org/abs/2410.11758", days_ago=1)
    p2, _ = _fake_arxiv_paper("https://arxiv.org/abs/2411.00001", days_ago=2,
                              title="Survey on Foo")
    with patch("src.discovery_sources.arxiv_recent.get_latest_embodied_ai_papers",
               return_value=[p1, p2]):
        from src.discovery_sources import fetch_arxiv_recent
        cands = fetch_arxiv_recent(tags=["cs.RO"], days=7, limit=10)
    assert len(cands) == 2
    assert cands[0].arxiv_id == "2410.11758"
    assert cands[0].source == "arxiv"
    assert cands[0].github_repo is None  # arxiv 不给 github 字段
    assert cands[0].submitted_date == day1


def test_gather_arxiv_filters_outside_days_window():
    """超出 days 窗口的论文会被 cutoff 过滤掉(只保留窗口内的)。"""
    fresh, _ = _fake_arxiv_paper("https://arxiv.org/abs/2410.11758", days_ago=1)
    stale, _ = _fake_arxiv_paper("https://arxiv.org/abs/2411.00001", days_ago=40,
                                 title="Old Paper")
    with patch("src.discovery_sources.arxiv_recent.get_latest_embodied_ai_papers",
               return_value=[fresh, stale]):
        from src.discovery_sources import fetch_arxiv_recent
        cands = fetch_arxiv_recent(tags=["cs.RO"], days=7, limit=10)
    assert [c.arxiv_id for c in cands] == ["2410.11758"]


# ---------------------------------------------------------------------
# 4-5. 硬过滤
# ---------------------------------------------------------------------

def test_prefilter_require_github():
    """无 github_repo 的候选被过滤。"""
    from src.paper_discovery import _prefilter, _load_profile, _default_profile_path
    profile = _load_profile(_default_profile_path())
    profile["require_github"] = True
    cs = [
        _make_candidate(arxiv_id="2410.11758", github_repo="https://github.com/a/b"),
        _make_candidate(arxiv_id="2410.99999", github_repo=None),
    ]
    out = _prefilter(cs, profile)
    assert len(out) == 1
    assert out[0].arxiv_id == "2410.11758"


def test_prefilter_exclude_survey():
    """title 含 'A Survey on' 被排除。"""
    from src.paper_discovery import _prefilter, _load_profile, _default_profile_path
    profile = _load_profile(_default_profile_path())
    profile["require_github"] = False  # 隔离 github 因素
    cs = [
        _make_candidate(arxiv_id="1", title="A Survey on Embodied AI",
                        github_repo="https://github.com/a/b"),
        _make_candidate(arxiv_id="2", title="Embodied VLA Robot",
                        github_repo="https://github.com/a/b"),
    ]
    out = _prefilter(cs, profile)
    titles = [c.title for c in out]
    assert "A Survey on Embodied AI" not in titles
    assert "Embodied VLA Robot" in titles


# ---------------------------------------------------------------------
# 6-7. 去重
# ---------------------------------------------------------------------

def test_dedupe_exact_arxiv_id(tmp_path):
    """published.json 含同 arxiv_id → 过滤。"""
    from src.paper_discovery import _dedupe_against_published
    state = tmp_path / "published.json"
    state.write_text(json.dumps([
        {"arxiv_id": "2410.11758", "title": "old"}
    ]), encoding="utf-8")
    cs = [
        _make_candidate(arxiv_id="2410.11758"),
        _make_candidate(arxiv_id="2410.99999"),
    ]
    out = _dedupe_against_published(cs, str(state))
    assert len(out) == 1
    assert out[0].arxiv_id == "2410.99999"


def test_dedupe_title_fuzzy_threshold(tmp_path):
    """ratio 0.9 → 过滤；ratio 0.7 → 留。"""
    from src.paper_discovery import _dedupe_against_published
    state = tmp_path / "published.json"
    state.write_text(json.dumps([
        {"arxiv_id": "2410.11758",
         "title": "Embodied VLA Robot Manipulation"}
    ]), encoding="utf-8")
    # near-identical（应过滤）
    near = _make_candidate(arxiv_id="2410.11999",
                           title="Embodied VLA Robot Manipulations")
    # 不同主题（应留）
    diff = _make_candidate(arxiv_id="2410.22222",
                           title="A Completely Different Topic About Astronomy")
    out = _dedupe_against_published([near, diff], str(state))
    ids = [c.arxiv_id for c in out]
    assert "2410.11999" not in ids
    assert "2410.22222" in ids


# ---------------------------------------------------------------------
# 8-9. 启发式打分
# ---------------------------------------------------------------------

def test_heuristic_rank_topic_match():
    """命中多个 topic 的候选 score > 命中 0 个的。"""
    from src.paper_discovery import _heuristic_rank, _load_profile, _default_profile_path
    profile = _load_profile(_default_profile_path())
    profile["min_score"] = 0  # 关闭低分阈
    hit2 = _make_candidate(arxiv_id="A", title="Embodied VLA Robot",
                           abstract="imitation learning manipulation")
    hit0 = _make_candidate(arxiv_id="B", title="Pure Algebraic Topology",
                           abstract="something irrelevant")
    res_hit = _heuristic_rank([hit2], profile, topic="embodied")
    res_miss = _heuristic_rank([hit0], profile, topic="embodied")
    assert res_hit["score"] > res_miss["score"]
    assert res_hit["candidate"].arxiv_id == "A"


def test_heuristic_rank_min_score_triggers_fallback():
    """全部 < min_score → candidate=None, reason='below_min_score'。"""
    from src.paper_discovery import _heuristic_rank, _load_profile, _default_profile_path
    profile = _load_profile(_default_profile_path())
    profile["min_score"] = 999.0  # 极高阈值，谁都打不到
    cs = [_make_candidate(arxiv_id="X", title="Embodied", abstract="")]
    res = _heuristic_rank(cs, profile, topic="embodied")
    assert res["candidate"] is None
    assert res["reason"] == "below_min_score"


# ---------------------------------------------------------------------
# 10-11. opencode rank
# ---------------------------------------------------------------------

def test_opencode_rank_mock_returns_json():
    """mock subprocess.run 返回 ```json {...}``` → 正确 parse。"""
    from src.paper_discovery import _opencode_rank, _load_profile, _default_profile_path
    profile = _load_profile(_default_profile_path())
    cs = [
        _make_candidate(arxiv_id="A", title="VLA Robot"),
        _make_candidate(arxiv_id="B", title="Imitation Learning"),
    ]
    fake_stdout = "some chatter\n```json\n{\"index\": 1, \"score\": 8, \"reason\": \"opencode pick B\"}\n```\nmore log"
    fake_proc = MagicMock(stdout=fake_stdout, stderr="", returncode=0)
    with patch("src.paper_discovery.subprocess.run", return_value=fake_proc) as m, \
         patch.dict(os.environ, {"JSR_DISCOVERY_DISABLE_OPENCODE": ""}):
        out = _opencode_rank(cs, profile, topic="embodied",
                             opencode_model="deepseek-model1/model1")
    assert m.called
    assert out is not None
    assert out["candidate"].arxiv_id == "B"
    assert out["score"] == 8.0
    assert out["reason"].startswith("opencode:")


def test_opencode_rank_failure_falls_back_to_heuristic():
    """subprocess.run 抛 TimeoutExpired → opencode_rank 返回 None,
    上层 discover_top_paper 应继续走启发式。"""
    from src.paper_discovery import _opencode_rank, _load_profile, _default_profile_path
    profile = _load_profile(_default_profile_path())
    cs = [_make_candidate(arxiv_id="A", title="VLA Robot")]
    with patch("src.paper_discovery.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd="opencode", timeout=180)), \
         patch.dict(os.environ, {"JSR_DISCOVERY_DISABLE_OPENCODE": ""}):
        out = _opencode_rank(cs, profile, topic="embodied",
                             opencode_model="deepseek-model1/model1")
    assert out is None


# ---------------------------------------------------------------------
# 12. 集成（全 mock 链路）
# ---------------------------------------------------------------------

def test_discover_top_paper_integration_mock_all(tmp_path):
    """全 mock：sources / opencode → 拿到合法 dict，含 url / arxiv_id。"""
    from src import paper_discovery as pd

    fake_hf = [
        _make_candidate(arxiv_id="2410.11758",
                        title="Embodied VLA Robot Manipulation",
                        abstract="imitation learning",
                        submitted_date="2026-04-22",
                        upvotes=10,
                        github_repo="https://github.com/a/b",
                        project_page="https://x.example.com",
                        source="hf"),
    ]
    fake_arxiv = []  # arxiv 数据源给空，确保走 hf

    # 隔离 state path 到 tmp（避免污染真实 cache）
    with patch.object(pd, "_state_path",
                      return_value=str(tmp_path / "published.json")), \
         patch("src.paper_discovery._gather_candidates",
               return_value=fake_hf + fake_arxiv), \
         patch.object(pd, "_run_opencode_with_pipe",
                      return_value="```json\n{\"index\": 0, \"score\": 9, \"reason\": \"top pick\"}\n```"), \
         patch.dict(os.environ, {"JSR_DISCOVERY_DISABLE_OPENCODE": ""}):
        result = pd.discover_top_paper(topic="embodied",
                                       sources=["hf", "arxiv"])
    assert isinstance(result, dict)
    assert result["arxiv_id"] == "2410.11758"
    assert result["url"].startswith("https://arxiv.org/abs/")
    assert result["score"] == 9.0
    assert "opencode" in result["reason"]


# ---------------------------------------------------------------------
# 13-14. _append_published
# ---------------------------------------------------------------------

def test_append_published_creates_file_if_missing(tmp_path):
    from src.paper_discovery import _append_published
    state = tmp_path / "sub" / "published.json"
    _append_published(str(state), {
        "arxiv_id": "2410.11758",
        "title": "Test",
        "date": "2026-04-24",
        "video_path": "/tmp/x.mp4",
    })
    assert state.exists()
    data = json.loads(state.read_text(encoding="utf-8"))
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["arxiv_id"] == "2410.11758"


def test_append_published_dedup_idempotent(tmp_path):
    """同 arxiv_id append 两次 → 只 1 条。"""
    from src.paper_discovery import _append_published
    state = tmp_path / "published.json"
    payload = {"arxiv_id": "2410.11758", "title": "Test", "date": "2026-04-24"}
    _append_published(str(state), payload)
    _append_published(str(state), payload)
    data = json.loads(state.read_text(encoding="utf-8"))
    assert len(data) == 1


# ---------------------------------------------------------------------
# 15. opencode kill switch (extra)
# ---------------------------------------------------------------------

def test_opencode_disabled_via_env():
    """JSR_DISCOVERY_DISABLE_OPENCODE=1 → _opencode_rank 直接 None。"""
    from src.paper_discovery import _opencode_rank, _load_profile, _default_profile_path
    profile = _load_profile(_default_profile_path())
    cs = [_make_candidate(arxiv_id="A")]
    with patch.dict(os.environ, {"JSR_DISCOVERY_DISABLE_OPENCODE": "1"}):
        out = _opencode_rank(cs, profile, topic="embodied",
                             opencode_model="deepseek/x")
    assert out is None


# ---------------------------------------------------------------------
# 16. topic 全词边界匹配（Bug 2c 修复）
# ---------------------------------------------------------------------

def test_topic_match_word_boundary():
    """``topic="action"`` 应**不**误配 ``"Automation"``。"""
    from src.paper_discovery import _topic_match_count
    assert _topic_match_count("GUI Automation Framework", ["action"]) == 0
    assert _topic_match_count("latent action policy learning", ["action"]) == 1
    # 短语也走全词
    assert _topic_match_count("a latent action model", ["latent action"]) == 1
    assert _topic_match_count("latent_action model", ["latent action"]) == 0


def test_topic_match_chinese_substring():
    """中文 topic 走子串（无 \\b 边界）。"""
    from src.paper_discovery import _topic_match_count
    assert _topic_match_count("具身智能机器人", ["具身智能"]) == 1
    assert _topic_match_count("纯英文文本", ["具身智能"]) == 0


# ---------------------------------------------------------------------
# 17. _prefilter 在用户 topic 非空时硬过滤
# ---------------------------------------------------------------------

def test_prefilter_user_topic_required():
    """``topic="latent action"``，候选都不含 → 应全被过滤。"""
    from src.paper_discovery import _prefilter, _load_profile, _default_profile_path
    profile = _load_profile(_default_profile_path())
    profile["require_github"] = False  # 隔离 github 因素
    cs = [
        _make_candidate(arxiv_id="1", title="GUI Automation Framework",
                        abstract="some text", github_repo="https://x.com/a/b"),
        _make_candidate(arxiv_id="2", title="Robot Manipulation",
                        abstract="manip", github_repo="https://x.com/a/b"),
    ]
    out = _prefilter(cs, profile, topic="latent action")
    assert out == []

    # 加一篇真命中的，验证保留
    cs.append(_make_candidate(arxiv_id="3",
                              title="Latent Action Pretraining for Robots",
                              abstract="lap pretraining",
                              github_repo="https://x.com/a/b"))
    out2 = _prefilter(cs, profile, topic="latent action")
    assert len(out2) == 1
    assert out2[0].arxiv_id == "3"


# ---------------------------------------------------------------------
# 18. fetch_arxiv_recent 带 topic kwarg → arxiv API URL 含 abs:%22topic%22
# ---------------------------------------------------------------------

def test_arxiv_recent_topic_kwarg_url_encoded(monkeypatch):
    """直连 arxiv 路径下，URL 必含 ``abs:%22latent+action%22``。"""
    from src.discovery_sources import arxiv_recent as ar

    captured = {}

    class _FakeFeed:
        entries = []

    def _fake_parse(url):
        captured["url"] = url
        return _FakeFeed()

    # 强制直连 API 路径：让 mock-friendly 路径抛错
    def _raise(*a, **kw):
        raise RuntimeError("force direct API")

    monkeypatch.setattr(ar, "get_latest_embodied_ai_papers", _raise)
    # patch 模块内 import 出来的 feedparser.parse
    import feedparser
    monkeypatch.setattr(feedparser, "parse", _fake_parse)

    out = ar.fetch_arxiv_recent(tags=["cs.RO", "cs.CV"], days=7, limit=10,
                                 topic="latent action")
    assert isinstance(out, list)
    url = captured.get("url", "")
    assert "search_query=" in url
    # 短语 latent action → quote_plus("\"latent action\"") = %22latent+action%22
    assert "abs:%22latent+action%22" in url, "actual URL: " + url
    assert "cat:cs.RO" in url and "cat:cs.CV" in url


def test_arxiv_recent_no_topic_backward_compat(monkeypatch):
    """没传 topic → URL 仍是纯 cat 查询，不含 abs:。"""
    from src.discovery_sources import arxiv_recent as ar

    captured = {}

    class _FakeFeed:
        entries = []

    def _fake_parse(url):
        captured["url"] = url
        return _FakeFeed()

    def _raise(*a, **kw):
        raise RuntimeError("force direct API")

    monkeypatch.setattr(ar, "get_latest_embodied_ai_papers", _raise)
    import feedparser
    monkeypatch.setattr(feedparser, "parse", _fake_parse)

    ar.fetch_arxiv_recent(tags=["cs.RO"], days=7, limit=10)
    url = captured.get("url", "")
    assert "search_query=cat:cs.RO" in url
    assert "abs:" not in url
