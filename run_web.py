#!/usr/bin/env python3
"""Entry point for the Expense Tracker web UI.

Host and port come from ``[web]`` in config.toml (default 127.0.0.1:8000).
Pass ``--dev`` to auto-reload on code changes.
"""

import argparse
import logging

import uvicorn

from core.config import get_config
from core.database import init_db

log = logging.getLogger("run_web")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dev", action="store_true", help="reload when source files change")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    config = get_config()
    if config.source:
        log.info("Using settings from %s", config.source)
    if not config.web_is_local:
        log.warning(
            "[web] host is %s, which is not a loopback address: anyone who can reach "
            "this machine on port %d can read your transactions.",
            config.web.host,
            config.web.port,
        )
    if not config.llm_is_local:
        log.warning(
            "[llm] host is %s, which is not local: statement text will leave this machine.",
            config.llm.host,
        )

    # Ensure database is initialised (and migrated) before serving.
    init_db()

    uvicorn.run(
        "web.app:app",
        host=config.web.host,
        port=config.web.port,
        reload=args.dev,
        log_level="info",
    )


if __name__ == "__main__":
    main()
