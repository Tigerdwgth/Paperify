"""pytest 全局配置 + 真实出口防线。

两道 autouse 防线默认对所有测试生效, 想跑真实链路必须显式加 marker 放行:

1. **网络防线**(``allow_network`` 放行): 任何指向非 localhost 的 socket 连接 /
   HTTP 请求立刻抛 :class:`BlockedRealCall`, 而不是挂在那里等超时。
   localhost 放行——chromium CDP 探活、小红书 MCP 容器探活都依赖本地回环。
2. **发布防线**(``allow_publish`` / ``allow_browser`` 放行): B站 / 小红书 / 抖音
   的真实上传实现, 以及小红书图文卡片的 chromium 渲染层, 默认被换成抛错桩。
   小红书 MCP 服务跑在 ``localhost:18060``, 网络防线按设计拦不住它,
   这道防线才是"测试绝不碰线上发布接口"的真正保证。

``smoke`` 标记的测试(默认 skip, ``-m smoke`` 才跑)天然豁免两道防线。
"""
from __future__ import annotations

import importlib
import os
import socket
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class BlockedRealCall(BaseException):
    """测试打到真实网络 / 真实发布接口时抛出。

    故意继承 ``BaseException``: 业务代码里到处是 ``except Exception`` 兜底,
    继承 ``Exception`` 会被吞掉变成一条 warning, 漏网调用就看不见了。
    """


# ---------------------------------------------------------------------------
# marker 注册 + smoke 默认跳过
# ---------------------------------------------------------------------------
_MARKERS = (
    "smoke: 真实第三方 API 调用 (默认 skip, -m smoke 才跑)",
    "slow: 耗时测试 (测试内部用 JSR_RUN_SLOW_TESTS 环境变量放行)",
    "allow_network: 放行真实外网出口 (默认被 conftest 掐断)",
    "allow_publish: 放行真实发布/上传实现 (默认被 conftest 掐断)",
    "allow_browser: 放行真实 chromium 渲染 (默认被 conftest 掐断)",
)


def pytest_configure(config):
    for line in _MARKERS:
        config.addinivalue_line("markers", line)


def pytest_collection_modifyitems(config, items):
    """默认跳过 smoke 标记的测试; 仅在 -m smoke 显式选择时才执行."""
    keyword = (config.getoption("-m") or "").strip()
    if "smoke" in keyword:
        return
    skip_smoke = pytest.mark.skip(reason="smoke test (需 -m smoke 显式开启)")
    for item in items:
        if "smoke" in item.keywords:
            item.add_marker(skip_smoke)


def _has_marker(request, *names) -> bool:
    return any(request.node.get_closest_marker(name) for name in names)


# ---------------------------------------------------------------------------
# 防线 1: 真实网络出口
# ---------------------------------------------------------------------------
_LOCAL_HOSTS = {
    "", "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "::1", "0.0.0.0", "::",
}
_AF_UNIX = getattr(socket, "AF_UNIX", None)

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection


def _host_of(address) -> str:
    if isinstance(address, (tuple, list)) and address:
        return str(address[0])
    return str(address)


def _is_local_host(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    return host in _LOCAL_HOSTS or host.startswith("127.")


def _is_local_address(sock, address) -> bool:
    # AF_UNIX / 抽象命名空间: 本机 IPC, 放行
    if _AF_UNIX is not None and getattr(sock, "family", None) == _AF_UNIX:
        return True
    if isinstance(address, (bytes, str)):
        return True
    return _is_local_host(_host_of(address))


@pytest.fixture(autouse=True)
def _guard_real_network(request, monkeypatch):
    """把非 localhost 的网络出口掐断, 漏网调用立刻失败而不是挂起。"""
    if _has_marker(request, "smoke", "allow_network"):
        return

    node_id = request.node.nodeid

    def _blocked(target: str):
        raise BlockedRealCall(
            f"测试 {node_id} 试图访问真实网络: {target}\n"
            "请 mock 掉该调用; 确实需要外网的测试加 @pytest.mark.smoke "
            "或 @pytest.mark.allow_network。"
        )

    def guarded_connect(self, address, *args, **kwargs):
        if _is_local_address(self, address):
            return _real_connect(self, address, *args, **kwargs)
        _blocked(_host_of(address))

    def guarded_connect_ex(self, address, *args, **kwargs):
        if _is_local_address(self, address):
            return _real_connect_ex(self, address, *args, **kwargs)
        _blocked(_host_of(address))

    def guarded_create_connection(address, *args, **kwargs):
        if _is_local_host(_host_of(address)):
            return _real_create_connection(address, *args, **kwargs)
        _blocked(_host_of(address))

    monkeypatch.setattr(socket.socket, "connect", guarded_connect, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex, raising=False)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection, raising=False)

    # requests 走连接池, 命中已有连接时不会再调 socket.connect, 单独拦一层
    try:
        from urllib.parse import urlparse

        from requests import adapters as _requests_adapters
    except Exception:  # noqa: BLE001 - requests 未装就没这层
        return

    _real_send = _requests_adapters.HTTPAdapter.send

    def guarded_send(self, request_obj, *args, **kwargs):
        url = getattr(request_obj, "url", "") or ""
        try:
            host = urlparse(url).hostname or ""
        except Exception:  # noqa: BLE001
            host = ""
        if _is_local_host(host):
            return _real_send(self, request_obj, *args, **kwargs)
        _blocked(f"{getattr(request_obj, 'method', '?')} {url}")

    monkeypatch.setattr(_requests_adapters.HTTPAdapter, "send", guarded_send, raising=False)


# ---------------------------------------------------------------------------
# 防线 2: 真实发布接口 / 真实浏览器渲染
# ---------------------------------------------------------------------------
# (模块, 属性, 人话描述)
_PUBLISH_TARGETS = (
    ("src.distribution.orchestrator", "_upload_bilibili_impl", "B站视频上传"),
    ("src.distribution.orchestrator", "_upload_xiaohongshu_note_impl", "小红书图文发布"),
    ("src.distribution.orchestrator", "_upload_xiaohongshu_video_impl", "小红书视频发布"),
    ("src.distribution.orchestrator", "_upload_douyin_impl", "抖音视频上传"),
    # 小红书 MCP 服务在 localhost, 网络防线不拦, 这里按工具调用入口拦
    ("src.distribution.xiaohongshu", "_call_tool", "小红书 MCP 工具调用"),
    ("src.distribution.xiaohongshu", "ensure_mcp_service", "小红书 MCP 服务拉起(docker)"),
    # biliup 客户端: B站真实投稿都从这个类走
    ("src.distribution.bilibili", "BiliBili", "B站 biliup 客户端"),
)

_BROWSER_TARGETS = (
    ("src.distribution.xhs_cards", "_render_htmls_to_pngs", "小红书卡片 chromium 渲染"),
)

# 评论回复走 SAU venv 子进程(不是 HTTP), 网络防线按设计拦不住。
# 这些函数都留了 _runner 测试注入口: 注入了就是测试在跑, 放行; 没注入=真跑, 拦。
_RUNNER_SEAM_TARGETS = (
    ("src.distribution.comments.xhs_comments", "_run_helper", "小红书评论/回复 SAU 子进程"),
    ("src.distribution.comments.douyin_comments", "_run_reply_helper", "抖音评论回复 SAU 子进程"),
)


@pytest.fixture(scope="session")
def _guard_modules():
    """一次性导入被守护的模块(导入失败的置 None, 例如依赖缺失)。"""
    names = {
        t[0] for t in _PUBLISH_TARGETS + _BROWSER_TARGETS + _RUNNER_SEAM_TARGETS
    }
    mods = {}
    for name in sorted(names):
        try:
            mods[name] = importlib.import_module(name)
        except Exception:  # noqa: BLE001
            mods[name] = None
    return mods


def _install_boom(monkeypatch, module, attr, label, node_id, marker_hint):
    def boom(*args, **kwargs):
        raise BlockedRealCall(
            f"测试 {node_id} 试图调用真实{label} ({module.__name__}.{attr})\n"
            f"请在测试里 mock 掉它; 确实要打真实链路请加 @pytest.mark.smoke "
            f"或 @pytest.mark.{marker_hint}。"
        )

    boom.__name__ = f"blocked_{attr}"
    monkeypatch.setattr(module, attr, boom, raising=False)


def _install_seam_guard(monkeypatch, module, attr, label, node_id):
    """带 ``_runner`` 注入口的子进程封装: 注入了放行, 没注入就拦。"""
    original = getattr(module, attr)

    def guarded(*args, **kwargs):
        if kwargs.get("_runner") is not None:
            return original(*args, **kwargs)
        raise BlockedRealCall(
            f"测试 {node_id} 试图调用真实{label} ({module.__name__}.{attr})\n"
            f"请传 _runner=<假 runner> 或 mock 掉调用方; 真要打真实链路加 "
            f"@pytest.mark.smoke 或 @pytest.mark.allow_publish。"
        )

    guarded.__name__ = f"guarded_{attr}"
    monkeypatch.setattr(module, attr, guarded, raising=False)


@pytest.fixture(autouse=True)
def _guard_real_publish(request, monkeypatch, _guard_modules):
    """把 B站/小红书/抖音真实上传实现与 chromium 渲染换成抛错桩。"""
    node_id = request.node.nodeid
    block_publish = not _has_marker(request, "smoke", "allow_publish")
    block_browser = not _has_marker(request, "smoke", "allow_browser")

    targets = []
    if block_publish:
        targets += [(t, "allow_publish") for t in _PUBLISH_TARGETS]
    if block_browser:
        targets += [(t, "allow_browser") for t in _BROWSER_TARGETS]

    for (mod_name, attr, label), marker_hint in targets:
        module = _guard_modules.get(mod_name)
        if module is None or not hasattr(module, attr):
            continue
        _install_boom(monkeypatch, module, attr, label, node_id, marker_hint)

    if not block_publish:
        return
    for mod_name, attr, label in _RUNNER_SEAM_TARGETS:
        module = _guard_modules.get(mod_name)
        if module is None or not hasattr(module, attr):
            continue
        _install_seam_guard(monkeypatch, module, attr, label, node_id)
