"""Create synthetic UI acceptance accounts, never from a production backup."""

import argparse
import json
import secrets
import uuid
from pathlib import Path
from contextlib import closing
from app import migration as m, auth
from app.account_admin import create_user


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--database", type=Path, required=True)
    p.add_argument("--credentials", type=Path, required=True)
    args = p.parse_args()
    password = secrets.token_urlsafe(24)
    m.initialize_empty_v1(args.database)
    owner = auth.create_owner(args.database, "ui-owner", "UI owner", password)
    service = auth.AuthService(args.database)
    ids = {
        n: create_user(service, "ui-" + n, "UI " + n, password)["user_id"]
        for n in ("owner2", "editor", "viewer", "other", "empty", "new", "disabled")
    }
    a = owner["workspace_id"]
    b = str(uuid.uuid4())
    stamp = auth.encoded_time(service.clock())
    with closing(m.connect(args.database)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE workspaces SET name='Workspace A' WHERE id=?", (a,))
        db.execute(
            "INSERT INTO workspaces VALUES(?,?,?,?,?,?)", (b, "Workspace B", ids["owner2"], 0, stamp, stamp)
        )
        for ws, uid, role in [
            (a, ids["owner2"], "owner"),
            (a, ids["editor"], "editor"),
            (a, ids["viewer"], "viewer"),
            (b, ids["owner2"], "owner"),
            (b, ids["editor"], "viewer"),
            (b, ids["other"], "viewer"),
        ]:
            db.execute(
                "INSERT INTO memberships VALUES(?,?,?,?,?,?,?)",
                (ws, uid, role, str(uuid.uuid4()), 1, stamp, stamp),
            )
        db.execute("UPDATE users SET status='disabled' WHERE id=?", (ids["disabled"],))
        db.execute("COMMIT")
        m.integrity(db)
    args.credentials.write_text(
        json.dumps({"password": password, "owner": owner, "users": ids, "A": a, "B": b}, indent=2),
        encoding="utf-8",
    )
    print("Synthetic fixture prepared. Credentials written to private path, never printed.")


if __name__ == "__main__":
    main()
