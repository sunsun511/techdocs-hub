"""Loopback test entrypoint with local-only boot evidence, never public debug API."""

import argparse
import json
import os
import time
import uuid
import uvicorn
from app import migration as m
from app.main import create_app
from app.config import Settings

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--database", required=True)
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args()
    db = m.safe_target(args.database)
    app = create_app(
        Settings(
            db_path=db, origin="http://127.0.0.1:" + str(args.port), models_enabled=False, worker_enabled=True
        )
    )
    marker = db.parent / f"service-ready-{args.port}-{os.getpid()}.json"
    marker.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "boot_id": str(uuid.uuid4()),
                "started_at": time.time(),
                "host": "127.0.0.1",
                "port": args.port,
            }
        ),
        encoding="utf-8",
    )
    uvicorn.run(
        app, host="127.0.0.1", port=args.port, access_log=False, log_level="error", proxy_headers=False
    )
