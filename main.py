#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DevEco2API 入口。"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from deveco2api.auth import ensure_auth
from deveco2api.config import load_config
from deveco2api.logger import setup_logger
from deveco2api.proxy import create_app

logger = setup_logger("deveco2api")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="DevEco2API: OpenAI 兼容 API 代理")
    parser.add_argument("--config", "-c", default="config.toml", help="配置文件路径")
    parser.add_argument("--host", help="监听 host")
    parser.add_argument("--port", type=int, help="监听 port")
    parser.add_argument("--no-browser", action="store_true", help="登录时不自动打开浏览器")
    parser.add_argument("--login", action="store_true", help="仅执行登录并保存 token")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path

    config = load_config(config_path)
    log_level = config.logging.get("level", "INFO")
    setup_logger("deveco2api", log_level)
    # 让子日志器也继承同一级别
    for child in ("deveco2api.auth", "deveco2api.proxy"):
        logging.getLogger(child).setLevel(log_level)

    if args.login:
        ensure_auth(config, str(config_path))
        return 0

    access_token = ensure_auth(config, str(config_path))
    logger.info("access_token 已就绪: %s...", access_token[:16])

    host = args.host or config.server.host
    port = args.port or config.server.port
    app = create_app(config, str(config_path))

    logger.info("启动 OpenAI 兼容 API: http://%s:%s/v1/chat/completions", host, port)
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
