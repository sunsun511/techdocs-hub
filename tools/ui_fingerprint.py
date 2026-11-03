"""Read fingerprints only inside explicitly configured disposable UI data root."""

import argparse
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from contextlib import closing

p = argparse.ArgumentParser()
p.add_argument("database", type=Path)
args = p.parse_args()
root = Path(os.environ["TECHDOCS_DATA_ROOT"]).resolve()
target = args.database.resolve()
assert target.is_relative_to(root)
with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True)) as db:
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    tables = [
        r[0]
        for r in db.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")
    ]
    out = {}
    for t in tables:
        rows = [
            json.dumps(
                [[type(v).__name__, v.hex() if isinstance(v, bytes) else v] for v in r], ensure_ascii=False
            )
            for r in db.execute('select * from "' + t + '"')
        ]
        out[t] = hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest()
    print(json.dumps(out))
    db.execute("COMMIT")
