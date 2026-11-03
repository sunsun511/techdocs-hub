"""Explicit local demo launcher; never initializes or migrates data."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--check-only", action="store_true")
    args = p.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    code = Path(__file__).resolve().parents[1]
    root = Path(config["data_root"]).resolve(strict=True)
    db = Path(config["database"]).resolve(strict=True)
    models = Path(config["models"]).resolve(strict=True)
    port = int(config["port"])
    if not 1024 <= port <= 65535:
        raise ValueError("Invalid loopback port")
    if root == code or root.is_relative_to(code) or not db.is_relative_to(root):
        raise ValueError("Demo requires an external dedicated data root")
    if (root / "DEMO_ONLY").read_text(encoding="utf-8").strip() != "synthetic-demo-only":
        raise ValueError("Dedicated synthetic demo marker required")
    if config.get("worker") is not True:
        raise ValueError("This demo explicitly requires its own worker")
    env = {
        **os.environ,
        "PYTHONPATH": str(code),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TECHDOCS_DATA_ROOT": str(root),
        "TECHDOCS_DB_PATH": str(db),
        "TECHDOCS_MODEL_DIR": str(models),
        "TECHDOCS_MODELS": "1",
        "TECHDOCS_WORKER": "1",
        "TECHDOCS_ORIGIN": f"http://127.0.0.1:{port}",
    }
    # Application path/marker protections remain enabled. No production path defaults.
    if args.check_only:
        print("Demo configuration valid; no service started")
        return
    with socket.socket() as guard:
        guard.bind(("127.0.0.1", port))
    print(f"Demo only: http://127.0.0.1:{port} ; Ctrl+C to stop", flush=True)
    command = [
        sys.executable,
        "-B",
        "-m",
        "tools.drill_server",
        "--database",
        str(db),
        "--models",
        str(models),
        "--port",
        str(port),
        "--boot",
        str(root / "boot.json"),
        "--worker",
    ]
    try:
        result = subprocess.run(command, cwd=code, env=env, check=False)
        raise SystemExit(result.returncode)
    except KeyboardInterrupt:
        print("Demo stopped by operator")


if __name__ == "__main__":
    main()
