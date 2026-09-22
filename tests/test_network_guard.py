"""tests/conftest.py 两道防线自身的回归测试。

防线坏掉 = 全量测试又能在无人察觉的情况下打真实网络 / 真实发布接口。
历史坑: 小红书分支改成"只发图文卡片"之后, 只 mock 了视频接口的测试一路跑到
真实 render_note_cards(起 chromium) + publish_note(打 MCP 真发笔记),
单条测试 90s 不返回, 全量 pytest 15 分钟只跑完 37 条。
"""
import socket
from types import SimpleNamespace

import pytest
import requests

from conftest import BlockedRealCall


# ---------------------------------------------------------------------------
# 网络防线
# ---------------------------------------------------------------------------
def test_external_http_request_is_blocked():
    with pytest.raises(BlockedRealCall):
        requests.get("https://example.com/api", timeout=1)


def test_external_socket_connection_is_blocked():
    with pytest.raises(BlockedRealCall):
        socket.create_connection(("example.com", 80), timeout=1)


def test_external_raw_socket_connect_is_blocked():
    sock = socket.socket()
    try:
        with pytest.raises(BlockedRealCall):
            sock.connect(("93.184.216.34", 80))
    finally:
        sock.close()


def test_localhost_connection_is_allowed():
    """本地回环要放行: chromium CDP / 小红书 MCP 容器的探活都走它。"""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=2)
        conn, _ = server.accept()
        conn.close()
        client.close()
    finally:
        server.close()


# ---------------------------------------------------------------------------
# 发布防线
# ---------------------------------------------------------------------------
def test_real_xhs_note_publish_impl_is_blocked():
    from src.distribution import orchestrator

    with pytest.raises(BlockedRealCall):
        orchestrator.upload_xiaohongshu_note(title="t", content="c",
                                             images=["/tmp/x.png"], tags=[])


def test_real_bilibili_upload_impl_is_blocked():
    from src.distribution import orchestrator

    with pytest.raises(BlockedRealCall):
        orchestrator.upload_bilibili(video_path="/tmp/x.mp4", title="t",
                                     tags="a", desc="d", cover_path=None, tid=188)


def test_unmocked_xhs_branch_cannot_reach_real_publish(tmp_path, monkeypatch):
    """没 mock 的小红书上传: 卡片渲染(chromium)这一层就被拦, 不会静默真发。"""
    from src.distribution.orchestrator import upload_generated_content

    monkeypatch.chdir(tmp_path)  # 隔离掉仓库里的 ./pic/*.png
    with pytest.raises(BlockedRealCall):
        upload_generated_content(
            platforms=["xiaohongshu"],
            video_path="",
            cover_path=None,
            video_title="测试",
            video_tags="",
            video_desc="",
            cn_titles=["中文标题"],
            summaries=["一段摘要。"],
            narration_segments=["一段旁白。"],
        )


def test_comment_helper_subprocess_is_blocked():
    """评论回复走 SAU 子进程(不是 HTTP), 网络防线拦不住, 由 seam 防线兜住。"""
    from src.distribution.comments import xhs_comments

    with pytest.raises(BlockedRealCall):
        xhs_comments._run_helper(["pull", "--note-id", "x"])


def test_comment_helper_allows_injected_runner():
    """注入 _runner 的测试用法不能被防线误伤。"""
    from src.distribution.comments import xhs_comments

    def fake_runner(cmd, **kwargs):
        return SimpleNamespace(stdout='{"ok": true}', stderr="", returncode=0)

    assert xhs_comments._run_helper(["pull"], _runner=fake_runner) == {"ok": True}


# ---------------------------------------------------------------------------
# 防线自身的防线
# ---------------------------------------------------------------------------
# conftest 用 monkeypatch.setattr(..., raising=False) 安装抛错桩。好处是模块缺依赖
# 时不会把整个 fixture 炸掉, 代价是: 谁把 _upload_xiaohongshu_note_impl 之类的名字
# 改了, setattr 会默默**新建**一个同名属性, 真实实现毫发无损 —— 防线变成空壳, 而
# 全量测试依旧全绿。那正是最危险的状态: 下一次跑测试就把笔记发到线上去了。
# 所以这里把「每个防线目标必须真实存在」本身变成一条会红的测试。


def _guard_target_table():
    """从 conftest 取防线目标表, 避免在测试里复制一份(复制就会不同步)。"""
    import conftest as conftest_mod

    return (
        [(m, a, d, "publish") for m, a, d in conftest_mod._PUBLISH_TARGETS]
        + [(m, a, d, "browser") for m, a, d in conftest_mod._BROWSER_TARGETS]
        + [(m, a, d, "seam") for m, a, d in conftest_mod._RUNNER_SEAM_TARGETS]
    )


def test_guard_targets_all_exist():
    """防线目标一旦被重命名/删除, 这里立刻红, 而不是静默失效。"""
    import importlib

    missing = []
    for module_name, attr, desc, kind in _guard_target_table():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001
            missing.append(f"[{kind}] {module_name} 导入失败({desc}): {exc}")
            continue
        if not hasattr(module, attr):
            missing.append(f"[{kind}] {module_name}.{attr} 不存在({desc})")

    assert not missing, (
        "conftest 的真实调用防线有目标已失效, 对应链路现在是裸奔状态:\n  "
        + "\n  ".join(missing)
        + "\n请修正 tests/conftest.py 里的目标表(改名后同步), 不要直接删除条目。"
    )


def test_guard_seam_targets_accept_runner_kwarg():
    """seam 防线靠 `_runner=` 区分测试与真跑, 被守护函数必须真支持这个参数。"""
    import importlib
    import inspect

    import conftest as conftest_mod

    bad = []
    for module_name, attr, desc in conftest_mod._RUNNER_SEAM_TARGETS:
        module = importlib.import_module(module_name)
        # 防线已把它换成 guarded 包装, 取 __wrapped__ 之外只能看原实现的签名,
        # 所以这里直接从未打补丁的模块源码层面拿: guarded 闭包捕获的 original。
        func = getattr(module, attr)
        sig = inspect.signature(func)
        has_runner = "_runner" in sig.parameters or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if not has_runner:
            bad.append(f"{module_name}.{attr}({desc}) 不接受 _runner 参数")

    assert not bad, (
        "seam 防线依赖 `_runner=` 注入口, 以下函数已不支持它, 防线判据失真:\n  "
        + "\n  ".join(bad)
    )
