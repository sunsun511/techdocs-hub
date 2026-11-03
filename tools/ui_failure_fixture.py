"""Prepare a failed-version UI fixture only in a disposable UI database."""

import argparse
import json
import os
import uuid
import hashlib
from pathlib import Path
from contextlib import closing
from app import migration as m, auth

p = argparse.ArgumentParser()
p.add_argument("database", type=Path)
p.add_argument("document")
args = p.parse_args()
target = args.database.resolve()
root = Path(os.environ["TECHDOCS_DATA_ROOT"]).resolve()
assert target.is_relative_to(root) and (root / "credentials.json").is_file()
with closing(m.connect(target)) as db:
    db.row_factory = __import__("sqlite3").Row
    db.execute("BEGIN IMMEDIATE")
    d = dict(db.execute("SELECT * FROM documents WHERE id=?", (args.document,)).fetchone())
    v = dict(db.execute("SELECT * FROM versions WHERE id=?", (d["active_version_id"],)).fetchone())
    t = dict(db.execute("SELECT * FROM tasks WHERE version_id=?", (v["id"],)).fetchone())
    vid = uuid.uuid4().hex
    tid = uuid.uuid4().hex
    revision = d["desired_revision"] + 1
    stamp = auth.encoded_time(__import__("time").time())
    v.update(
        id=vid,
        label="failed-fixture",
        content="Recovered retention documentation.",
        content_hash=hashlib.sha256(b"Recovered retention documentation.").hexdigest(),
        revision=revision,
        status="failed",
        chunk_count=0,
        created_at=stamp,
    )
    t.update(
        id=tid,
        version_id=vid,
        revision=revision,
        state="failed",
        error="Controlled UI failure fixture",
        created_at=stamp,
        updated_at=stamp,
    )
    for table, row in [("versions", v), ("tasks", t)]:
        db.execute(
            "INSERT INTO "
            + table
            + "("
            + ",".join('"' + k + '"' for k in row)
            + ") VALUES("
            + ",".join("?" for _ in row)
            + ")",
            tuple(row.values()),
        )
    db.execute("UPDATE documents SET desired_revision=? WHERE id=?", (revision, d["id"]))
    db.execute("COMMIT")
    db.row_factory = None
    m.integrity(db)
print(json.dumps({"id": tid, "old_active_version": d["active_version_id"]}))
