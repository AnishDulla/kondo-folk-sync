from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8787"))
    reload = os.environ.get("KONDO_FOLK_RELOAD", "true").lower() in {"1", "true", "yes", "on"}
    uvicorn.run("kondo_folk_sync.service:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
