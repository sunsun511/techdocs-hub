"""Initialize only a new disposable acceptance volume, password via stdin."""

import argparse
import json
import sys
from app import auth, migration


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--disposable", action="store_true", required=True)
    args = parser.parse_args()
    try:
        value = json.loads(sys.stdin.read(1024))
        migration.initialize_empty_v1(args.database)
        owner = auth.create_owner(
            args.database, "test-owner", "Disposable container owner", value["password"]
        )
        print(json.dumps({"owner_id": owner["owner_id"], "workspace_id": owner["workspace_id"]}))
    except Exception:
        print("Disposable initialization failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
