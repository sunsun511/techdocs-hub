"""Private boot evidence for an owned loopback drill service, B1 or B2."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import uvicorn
from app.config import Settings
from app.main import create_app
import app.search as search_module
import app.main as main_module


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--models", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--boot", required=True, type=Path)
    parser.add_argument("--worker", action="store_true")
    args = parser.parse_args()
    origin = "http://127.0.0.1:" + str(args.port)
    os.environ["TECHDOCS_ORIGIN"] = origin
    settings = Settings.from_env()
    from dataclasses import replace

    settings = replace(
        settings,
        db_path=args.database,
        model_dir=args.models,
        models_enabled=True,
        worker_enabled=args.worker,
    )
    app = create_app(settings)
    args.boot.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "boot_id": str(uuid.uuid4()),
                "started_at": time.time(),
                "app_path": str(Path(main_module.__file__).resolve()),
                "search_path": str(Path(search_module.__file__).resolve()),
                "search_sha256": hashlib.sha256(Path(search_module.__file__).read_bytes()).hexdigest(),
                "database": str(args.database.resolve()),
                "worker_enabled": args.worker,
                "origin": origin,
            }
        ),
        encoding="utf-8",
    )
    uvicorn.run(
        app, host="127.0.0.1", port=args.port, access_log=False, log_level="error", proxy_headers=False
    )


if __name__ == "__main__":
    main()
