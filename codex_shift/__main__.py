"""命令行入口: python -m codex_shift [--config PATH]

加载配置并以 uvicorn 启动服务。
"""

from __future__ import annotations

import argparse
import logging
import sys

import uvicorn

from .config import load_config
from .server import create_app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-shift", description="Codex Responses 协议适配代理")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径(默认 config.yaml)")
    parser.add_argument("--log-level", default="info", help="日志级别")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 加载配置(失败则快速退出)
    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)
        return 1

    app = create_app(cfg, config_path=args.config)
    log = logging.getLogger("codex_shift")
    admin_host = "127.0.0.1" if cfg.host == "0.0.0.0" else cfg.host
    log.info("启动: 监听 %s:%s, 已配置 %d 个 provider", cfg.host, cfg.port, len(cfg.providers))
    log.info("控制台: http://%s:%s/admin", admin_host, cfg.port)
    for p in cfg.providers:
        log.info(
            "  provider=%s 出站=%s 上游=%s%s models=%s",
            p.name, p.outbound, p.base_url, p.path,
            (p.models if p.models else "(兜底匹配全部)"),
        )
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
