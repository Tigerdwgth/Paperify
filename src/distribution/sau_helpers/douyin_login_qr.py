#!/usr/bin/env python3
"""通过 CDP attach 到 chromium-daemon 完成抖音首次扫码登录。

容器自包含登录流程:
1. daemon 启动后 default context 是空 cookie, page nav 在 creator.douyin.com 登录页
2. 本脚本 connect_over_cdp 复用 daemon page, 提取二维码 PNG 写到 cache/douyin_qr.png
3. 用户在主机用图片查看器打开该 PNG, 抖音 APP 扫码
4. 本脚本轮询登录完成标志, 成功后调 context.storage_state 写 cookie 到约定路径
5. POST /reload-cookie 让 daemon 用新 cookie 重新 new_context

CLI:
  python -m src.distribution.sau_helpers.douyin_login_qr \
      --daemon-url http://chromium-daemon:9222 \
      --output cache/douyin_qr.png
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.request
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [douyin-login-qr] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_COOKIE = PROJECT_ROOT / "cache" / "douyin_cookies.json"


async def _import_sau_helpers(sau_dir: str):
    sau_path = Path(sau_dir).expanduser().resolve()
    if str(sau_path) not in sys.path:
        sys.path.insert(0, str(sau_path))
    from uploader.douyin_uploader.main import (  # type: ignore
        _extract_douyin_qrcode_src,
        _is_douyin_login_completed,
    )
    return _extract_douyin_qrcode_src, _is_douyin_login_completed


def _save_data_url_png(data_url: str, output: Path) -> None:
    """把 data:image/png;base64,... 写到磁盘。"""
    import base64
    if not data_url.startswith("data:"):
        raise ValueError(f"不是 data URL: {data_url[:80]}")
    header, b64 = data_url.split(",", 1)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(base64.b64decode(b64))


def _post_reload_cookie(daemon_health_url: str) -> None:
    """触发 daemon 用新 cookie 重新 new_context。失败仅打 warning, 不阻断登录流程。"""
    url = daemon_health_url.rstrip("/") + "/reload-cookie"
    req = urllib.request.Request(url, method="POST",
                                 data=b"{}",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            logger.info("reload-cookie ok: %s", body)
    except Exception as exc:
        logger.warning("reload-cookie 失败 (daemon 重启即可生效): %s", exc)


async def _login(args) -> dict:
    from src.distribution.sau_helpers.cdp_helpers import (
        get_cdp_page, navigate_and_wait, close_cdp,
    )
    extract_qr, is_logged_in = await _import_sau_helpers(args.sau_dir)

    pw, browser, context, page = await get_cdp_page(args.daemon_url)
    try:
        login_url = "https://creator.douyin.com/"
        await navigate_and_wait(page, login_url)
        await page.wait_for_timeout(2000)

        if await is_logged_in(page):
            logger.info("daemon 已经处于登录态: %s", page.url)
            return {"ok": True, "already_logged_in": True, "url": page.url}

        output = Path(args.output).expanduser().resolve()
        # 抖音二维码可能因刷新更新, 我们做最多 max_refresh 次重新提取
        cookie_target = Path(args.cookie_output).expanduser().resolve()
        max_checks = args.max_checks
        poll_interval = args.poll_interval
        last_src = ""
        for idx in range(max_checks):
            try:
                src = await extract_qr(page)
            except Exception as exc:
                logger.warning("提取二维码失败(第 %d 次): %s", idx, exc)
                src = ""
            if src and src != last_src:
                _save_data_url_png(src, output)
                last_src = src
                logger.info(
                    "二维码已保存: %s (在主机用图片查看器打开扫码; "
                    "约 %d 秒后自动刷新)",
                    output, poll_interval * 30,
                )

            if await is_logged_in(page):
                logger.info("登录成功: %s", page.url)
                cookie_target.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(cookie_target))
                logger.info("cookie 已写到 %s", cookie_target)
                _post_reload_cookie(args.daemon_health_url)
                return {"ok": True, "url": page.url,
                        "cookie": str(cookie_target)}

            # 自动刷新逻辑暂时禁用: 抖音登录页存在隐藏的"二维码失效"文本
            # 节点导致每轮误触发刷新, 用户来不及扫码. 二维码 90s 自然失效后,
            # 用户自行重跑脚本即可.
            await asyncio.sleep(poll_interval)

        return {"ok": False, "error": "扫码超时"}
    finally:
        await close_cdp(pw, browser)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--daemon-url", default=os.environ.get(
        "JSR_CHROMIUM_DAEMON_URL", "http://localhost:9222"))
    p.add_argument("--daemon-health-url", default=os.environ.get(
        "JSR_CHROMIUM_DAEMON_HEALTH_URL", "http://localhost:9223"))
    p.add_argument("--output",
                   default=str(PROJECT_ROOT / "cache" / "douyin_qr.png"))
    p.add_argument("--cookie-output", default=str(DEFAULT_COOKIE))
    p.add_argument("--sau-dir", default=os.environ.get(
        "SAU_DIR",
        "/home/jdh/Projects/VlogCutter/third_party/social-auto-upload",
    ))
    p.add_argument("--poll-interval", type=int, default=3)
    p.add_argument("--max-checks", type=int, default=120)
    args = p.parse_args()

    result = asyncio.run(_login(args))
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
