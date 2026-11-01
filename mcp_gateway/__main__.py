"""Command line interface: ``python -m mcp_gateway serve|gen-key|check-config``."""

from __future__ import annotations

import argparse
import logging
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp-gateway", description="Mini MCP Gateway")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve = sub.add_parser("serve", help="run the gateway")
    serve.add_argument("--config", "-c", action="append",
                       help="gateway YAML; repeat to layer overlays (default: $MCPGW_CONFIG, comma-separated)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--log-level", default="info")

    sub.add_parser("gen-key", help="generate an API key and its SHA-256 for the config")

    check = sub.add_parser("check-config", help="validate a config file and exit")
    check.add_argument("--config", "-c", action="append", required=True)

    args = parser.parse_args(argv)

    if args.cmd == "gen-key":
        from .auth import generate_api_key

        key, digest = generate_api_key()
        print(f"api key (give to the client, shown once): {key}")
        print(f"key_sha256 (put in gateway config):       {digest}")
        return 0

    from .config import load_config
    from .errors import ConfigError

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e.message}", file=sys.stderr)
        return 2

    if args.cmd == "check-config":
        print(f"ok: {len(config.servers)} servers, {len(config.roles)} roles, {len(config.api_keys)} api keys")
        return 0

    import uvicorn

    from .app import create_app

    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for noisy in ("httpx", "mcp.server.lowlevel.server", "mcp.server.streamable_http"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    uvicorn.run(
        create_app(config), host=args.host or config.host, port=args.port or config.port,
        log_level=args.log_level, proxy_headers=True, server_header=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
