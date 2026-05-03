"""Entry point: `python run.py` to launch the host manager.

Reads PORT and HOST from env (defaults: 127.0.0.1:8000).
"""
from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Open http://{host}:{port}")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
