"""``src.discovery_sources.cli`` subprocess 集成测试。

实跑 ``python -m src.discovery_sources.cli ...``，验证 stdout JSON 契约。
mock 不友好（CLI 重新拉新进程），所以三个子命令分别用 ``--limit 1`` /
``--limit 2`` 控制网络压力。失败时（网络不通 / 服务器 503）也允许 ``ok=false``，
但 stdout 必须仍是合法 JSON 且字段齐。

注意: 真打 arxiv / HuggingFace 的三个子命令标了 ``smoke``——子进程在
tests/conftest.py 的网络防线之外, 默认跑全量时不许它们摸真实外网
(实测 hf-daily 一条就要 40s), 需要时用 ``pytest -m smoke`` 显式跑。
argparse 层的契约测试不碰网络, 保持默认执行。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_cli(*args, timeout=60):
    """跑 cli 子进程，返回 (returncode, stdout, stderr)。"""
    cmd = [sys.executable, "-m", "src.discovery_sources.cli", *args]
    env = os.environ.copy()
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _assert_json_contract(stdout: str, stderr: str, returncode: int):
    """统一断言：stdout 是合法 JSON 且含 ok/papers 字段，rc 与 ok 一致。"""
    assert stdout.strip(), (
        "stdout 不应为空 (stderr=%s)" % stderr[-500:])
    payload = json.loads(stdout)  # 抛 JSONDecodeError 即测试失败
    assert "ok" in payload, payload
    assert "papers" in payload, payload
    assert isinstance(payload["papers"], list), payload
    if payload["ok"]:
        assert returncode == 0
        assert "count" in payload
        assert payload["count"] == len(payload["papers"])
    else:
        assert returncode == 1, payload
        assert "error" in payload, payload
    return payload


@pytest.mark.smoke
def test_cli_arxiv_search_returns_json():
    """``arxiv-search --query <topic> --limit 2`` 应返回 JSON contract。"""
    rc, out, err = _run_cli(
        "arxiv-search", "--query", "diffusion policy",
        "--days", "30", "--limit", "2",
        timeout=60,
    )
    payload = _assert_json_contract(out, err, rc)
    if payload["ok"]:
        assert len(payload["papers"]) <= 2
        for p in payload["papers"]:
            assert "arxiv_id" in p
            assert "title" in p
            assert "url" in p
            assert "source" in p


@pytest.mark.smoke
def test_cli_arxiv_by_venue_returns_json():
    """``arxiv-by-venue --venue RSS --year 2025 --limit 5`` 应返回 JSON contract。"""
    rc, out, err = _run_cli(
        "arxiv-by-venue", "--venue", "RSS", "--year", "2025",
        "--limit", "5",
        timeout=60,
    )
    payload = _assert_json_contract(out, err, rc)
    if payload["ok"]:
        for p in payload["papers"]:
            assert p["source"] == "arxiv_venue"


@pytest.mark.smoke
def test_cli_hf_daily_returns_json():
    """``hf-daily --limit 3`` 应返回 JSON contract。"""
    rc, out, err = _run_cli(
        "hf-daily", "--limit", "3",
        timeout=60,
    )
    payload = _assert_json_contract(out, err, rc)
    if payload["ok"]:
        assert len(payload["papers"]) <= 3
        for p in payload["papers"]:
            assert p["source"] == "hf"


def test_cli_unknown_subcommand_returns_error():
    """未知子命令 → argparse 直接 exit 2（系统层面），不走我们的 JSON 流。"""
    rc, out, err = _run_cli("totally-bogus-subcmd", timeout=15)
    assert rc != 0


def test_cli_missing_required_query_returns_error():
    """``arxiv-search`` 不传 --query → argparse exit 2。"""
    rc, out, err = _run_cli("arxiv-search", timeout=15)
    assert rc != 0
