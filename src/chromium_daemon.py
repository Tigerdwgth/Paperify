"""容器内长跑的 chromium daemon。

启动一个 patchright chromium 实例(带 --remote-debugging-port=9222), 加载抖音 cookie
(若存在), nav 到创作者中心保持登录态。所有抖音上传 / 评论回复 / 扫码登录子进程通过
CDP attach 复用同一个 page, 避免反复 launch + new_context 触发抖音风控, 同时绕开
"context.storage_state(path=...) 写回会破坏 cookie" 的老坑。

健康检查 HTTP server 监听 :9223, 仅本机访问。

环境变量:
- JSR_CHROMIUM_DAEMON_CDP_PORT: chromium 的 CDP 端口, 默认 9222
- JSR_CHROMIUM_DAEMON_HEALTH_PORT: 健康检查端口, 默认 9223
- JSR_CHROMIUM_DAEMON_COOKIE: 抖音 storage_state JSON 路径,
  默认 cache/douyin_cookies.json (相对项目根)
- JSR_CHROMIUM_DAEMON_HOME_URL: 默认 https://creator.douyin.com/
- DISPLAY: Xvfb 显示号, 默认 :99 (本模块会自动 ensure)

CLI: python -m src.chromium_daemon
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [chromium-daemon] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("chromium_daemon")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CDP_PORT = int(os.environ.get("JSR_CHROMIUM_DAEMON_CDP_PORT", "9222"))
DEFAULT_HEALTH_PORT = int(os.environ.get("JSR_CHROMIUM_DAEMON_HEALTH_PORT", "9223"))
DEFAULT_COOKIE = Path(os.environ.get(
    "JSR_CHROMIUM_DAEMON_COOKIE",
    str(PROJECT_ROOT / "cache" / "douyin_cookies.json"),
))
DEFAULT_HOME_URL = os.environ.get(
    "JSR_CHROMIUM_DAEMON_HOME_URL",
    "https://creator.douyin.com/",
)
READY_FLAG = PROJECT_ROOT / "cache" / "chromium_daemon_ready"


def _ensure_xvfb(display: str = ":99") -> None:
    """Linux 上若 Xvfb 没在跑就启动。docker entrypoint 已起过, 这里幂等兜底。"""
    if platform.system() != "Linux":
        return
    if not shutil.which("Xvfb"):
        logger.warning("Xvfb 未安装, 假定已有可用 DISPLAY=%s", display)
        return
    rc = subprocess.run(["pgrep", "-f", f"Xvfb {display}"], capture_output=True)
    if rc.returncode == 0:
        return
    logger.info("启动 Xvfb %s", display)
    subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1920x1080x24", "-ac",
         "+extension", "RANDR"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)


_SAU_STEALTH_JS_PATH = "/app/third_party/social-auto-upload/utils/stealth.min.js"

def _load_stealth_js() -> str:
    """优先读 SAU stealth.min.js (180KB 业界标杆), 找不到时退化手写 fallback."""
    try:
        with open(_SAU_STEALTH_JS_PATH, "r", encoding="utf-8") as _sf:
            return _sf.read()
    except OSError:
        return """(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = window.chrome || { runtime: {}, app: {} };
    Object.defineProperty(navigator, 'plugins', {
      get: () => [{ name: 'Chrome PDF Plugin' }, { name: 'Chrome PDF Viewer' }]
    });
    Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
  } catch (e) {}
})();"""


_DOUYIN_LOGIN_COOKIES = ("sessionid_ss", "sessionid", "sid_guard")



class ChromiumDaemon:
    def __init__(
        self,
        cdp_port: int = DEFAULT_CDP_PORT,
        cookie_path: Path = DEFAULT_COOKIE,
        home_url: str = DEFAULT_HOME_URL,
    ):
        self.cdp_port = cdp_port
        self.cookie_path = cookie_path
        self.home_url = home_url
        self.status: str = "starting"
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        from patchright.async_api import async_playwright  # type: ignore
        self._pw = await async_playwright().start()
        launch_args = [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--window-size=1920,1080",
            f"--remote-debugging-port={self.cdp_port}",
            "--remote-debugging-address=0.0.0.0",
        ]
        logger.info("launching chromium, cdp_port=%d", self.cdp_port)
        self._browser = await self._pw.chromium.launch(
            headless=False,
            args=launch_args,
        )
        if self.cookie_path.exists():
            logger.info("加载 cookie storage_state: %s", self.cookie_path)
            self._context = await self._browser.new_context(
                storage_state=str(self.cookie_path),
                permissions=["geolocation"],
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/145.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            self.status = "ready"
        else:
            logger.warning("cookie 文件不存在(%s), 进入等待扫码登录态",
                           self.cookie_path)
            self._context = await self._browser.new_context(
                permissions=["geolocation"],
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/145.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
            )
            self.status = "logged_out"
        # stealth init script: 覆盖 webdriver / chrome.runtime / permissions /
        # plugins / languages, 让抖音的 navigator.webdriver === undefined detection 失效
        await self._context.add_init_script(_load_stealth_js())
        self._page = await self._context.new_page()
        await self._page.goto(self.home_url, wait_until="domcontentloaded",
                              timeout=30000)
        # 启动鼠标轨迹模拟 background task (人类行为, 让抖音不主动 expire QR)
        self._human_task = asyncio.create_task(self._human_loop())
        self._autosave_task = asyncio.create_task(self._autosave_cookie())
        READY_FLAG.parent.mkdir(parents=True, exist_ok=True)
        READY_FLAG.write_text(json.dumps({
            "status": self.status,
            "cdp_port": self.cdp_port,
            "pid": os.getpid(),
            "ts": time.time(),
        }))
        logger.info("daemon ready, status=%s url=%s", self.status, self._page.url)

    async def reload_cookie(self) -> dict:
        """读最新 cookie 文件, 关闭旧 context, 新建 context 替换。"""
        if not self.cookie_path.exists():
            return {"ok": False, "error": f"cookie 不存在: {self.cookie_path}"}
        old_context = self._context
        old_page = self._page
        new_context = await self._browser.new_context(
            storage_state=str(self.cookie_path),
            permissions=["geolocation"],
        )
        new_page = await new_context.new_page()
        await new_page.goto(self.home_url, wait_until="domcontentloaded",
                            timeout=30000)
        self._context = new_context
        self._page = new_page
        self.status = "ready"
        try:
            if old_page:
                await old_page.close()
            if old_context:
                await old_context.close()
        except Exception as exc:
            logger.warning("关闭旧 context 异常: %s", exc)
        return {"ok": True, "url": self._page.url}

    async def current_url(self) -> str:
        if self._page is None:
            return ""
        return self._page.url

    async def _human_loop(self) -> None:
        """背景模拟鼠标轨迹 + 偶尔滚轮: 让抖音 anti-bot 不强制 expire QR.

        每 2-6 秒随机鼠标移动 (steps=15-40 让 chromium 真的发送 mouse events,
        而非瞬移); 20% 概率轻微滚轮. 异常静默, 不影响主 daemon.
        """
        import random
        # 让 page 真的 navigate 完再开始, 避免 race
        await asyncio.sleep(3)
        while not self._stop.is_set():
            try:
                page = self._page
                if page is None:
                    await asyncio.sleep(2)
                    continue
                x = random.randint(200, 1700)
                y = random.randint(200, 900)
                steps = random.randint(15, 40)
                await page.mouse.move(x, y, steps=steps)
                if random.random() < 0.2:
                    dy = random.randint(-150, 150)
                    await page.mouse.wheel(0, dy)
            except Exception as exc:
                logger.debug("human_loop iter exc (ignored): %s", exc)
            await asyncio.sleep(random.uniform(2.0, 6.0))

    async def _autosave_cookie(self) -> None:
        """每 5 秒检查 context cookies, 发现抖音登录关键 cookie 立刻 storage_state.

        实现 "daemon 持有扫码 session" 模式: 用户扫码后 daemon 自动捕获 cookie 写到
        douyin_cookies.json, 切 status=ready. 后续 SAU upload attach daemon 直接用同
        一 session (cookie 不丢, 不需要 reload-cookie).
        """
        await asyncio.sleep(8)  # 等 page 第一次 nav 完
        saved = False
        while not self._stop.is_set():
            try:
                if self._context is None or saved:
                    await asyncio.sleep(5)
                    continue
                cookies = await self._context.cookies()
                names = {c.get("name") for c in cookies}
                hit = [n for n in _DOUYIN_LOGIN_COOKIES if n in names]
                if hit:
                    self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
                    await self._context.storage_state(path=str(self.cookie_path))
                    logger.info(
                        "[autosave] 检测到登录 cookie %s, 已写到 %s",
                        hit, self.cookie_path,
                    )
                    self.status = "ready"
                    saved = True
            except Exception as exc:
                logger.debug("[autosave] iter exc (ignored): %s", exc)
            await asyncio.sleep(5)

    async def stop(self) -> None:
        logger.info("daemon shutting down, 保存 cookie 到 %s", self.cookie_path)
        try:
            if self._context is not None and self.status == "ready":
                self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
                await self._context.storage_state(path=str(self.cookie_path))
        except Exception as exc:
            logger.error("保存 cookie 失败: %s", exc)
        try:
            if self._browser is not None:
                await self._browser.close()
        except Exception as exc:
            logger.error("关闭 browser 异常: %s", exc)
        try:
            if self._pw is not None:
                await self._pw.stop()
        except Exception as exc:
            logger.error("关闭 playwright 异常: %s", exc)
        if READY_FLAG.exists():
            READY_FLAG.unlink()
        self._stop.set()

    async def wait_until_stopped(self) -> None:
        await self._stop.wait()


def _build_app(daemon: ChromiumDaemon) -> web.Application:
    async def health(_request):
        return web.json_response({"status": daemon.status})

    async def current_url(_request):
        url = await daemon.current_url()
        return web.json_response({"url": url})

    async def reload_cookie(_request):
        result = await daemon.reload_cookie()
        return web.json_response(result, status=200 if result.get("ok") else 400)

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/current-url", current_url)
    app.router.add_post("/reload-cookie", reload_cookie)
    return app


async def _run(cdp_port: int, health_port: int) -> None:
    _ensure_xvfb(os.environ.get("DISPLAY", ":99"))
    daemon = ChromiumDaemon(cdp_port=cdp_port)
    await daemon.start()
    app = _build_app(daemon)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", health_port)
    await site.start()
    logger.info("health endpoint 监听 0.0.0.0:%d", health_port)

    loop = asyncio.get_running_loop()
    stopping = asyncio.Event()

    def _signal_handler(signame: str) -> None:
        logger.info("收到信号 %s, 准备退出", signame)
        stopping.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig.name)
        except NotImplementedError:
            pass

    await stopping.wait()
    await runner.cleanup()
    await daemon.stop()


def main() -> None:
    asyncio.run(_run(DEFAULT_CDP_PORT, DEFAULT_HEALTH_PORT))


if __name__ == "__main__":
    main()
