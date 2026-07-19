#!/usr/bin/env python3
"""Entry point for the Expense Tracker web UI."""

import uvicorn

from core.database import init_db

if __name__ == "__main__":
    # Ensure database is initialised
    init_db()

    uvicorn.run(
        "web.app:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
        log_level="info",
    )
