"""Portable isolated fixtures for pytest; never opens the user's database."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

CODE_ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ensure_context(evidence_root=None):
    configured = os.environ.get("TECHDOCS_ACCEPTANCE_ROOT")
    root = Path(configured).resolve() if configured else None
    if root is None and evidence_root and (Path(evidence_root) / "baseline.json").is_file():
        root = Path(evidence_root).resolve()
    if root is not None and (root / "baseline.json").is_file():
        baseline = json.loads((root / "baseline.json").read_text(encoding="utf-8"))
        os.environ["TECHDOCS_ACCEPTANCE_ROOT"] = str(root)
        os.environ.setdefault("TECHDOCS_DATA_ROOT", str(root / "private_data"))
        os.environ.setdefault("TECHDOCS_FORMAL_ROOT", baseline.get("reference_root", str(CODE_ROOT)))
        protected = [
            baseline["prior_backup_path"],
            str(Path(os.environ["TECHDOCS_FORMAL_ROOT"]) / "data/techdocs.sqlite3"),
        ]
        current = os.environ.get("TECHDOCS_PROTECTED_DATABASES", "")
        os.environ["TECHDOCS_PROTECTED_DATABASES"] = os.pathsep.join(filter(None, [current, *protected]))
        os.environ.setdefault("ACCOUNT_RUN_ID", "accounts-default")
        os.environ.setdefault("WORKSPACE_RUN_ID", "workspace-default")
        return root
    root = Path(tempfile.mkdtemp(prefix="techdocs-tests-"))
    private = root / "private_data"
    private.mkdir()
    os.environ["TECHDOCS_ACCEPTANCE_ROOT"] = str(root)
    os.environ["TECHDOCS_DATA_ROOT"] = str(private)
    from app.storage import Store
    from app import migration as m

    source = private / "baseline.sqlite3"
    store = Store(source)
    for label in ("v1", "v2"):
        payload = {
            "key": "historic-fixture",
            "title": "Historic fixture",
            "version_label": label,
            "source_url": "https://example.org/history",
            "format": "text",
            "content": "Preserved historic retention document " + label,
        }
        store.import_document(payload, "baseline-" + label)
        task = store.claim_task()
        store.publish(
            task,
            [{"heading": "History", "text": payload["content"], "vector": struct.pack("fff", 1, 0, 0)}],
            "synthetic-baseline-vector",
        )
    with m.connect(source, readonly=True, immutable=True) as db:
        m.preflight(db)
    # A clean, disposable reference repository is used by legacy protection
    # assertions. It is explicitly a fixture, never the user's formal checkout.
    reference = root / "reference"
    (reference / "data").mkdir(parents=True)
    shutil.copy2(source, reference / "data/techdocs.sqlite3")
    (reference / "README.md").write_text("Synthetic v1 reference for portable acceptance.\n")
    (reference / ".gitignore").write_text("data/\n")

    def git(*args):
        subprocess.run(["git", *args], cwd=reference, check=True, capture_output=True)

    git("init", "-q")
    git("add", "README.md", ".gitignore")
    git(
        "-c",
        "user.name=Test Fixture",
        "-c",
        "user.email=fixture@local.invalid",
        "commit",
        "-q",
        "-m",
        "Isolated reference",
    )

    def lock(paths):
        return {str(p): digest(p) for p in paths}

    import argon2

    baseline = {
        "fixture_baseline": True,
        "reference_root": str(reference),
        "prior_backup_path": str(source),
        "prior_backup_sha256": digest(source),
        "protected_files": lock(
            [reference / "README.md", reference / ".gitignore", reference / "data/techdocs.sqlite3"]
        ),
        "prior_migration_files": lock(
            [CODE_ROOT / "app/migration.py", CODE_ROOT / "app/db_policy.py", CODE_ROOT / "app/v1-schema.json"]
        ),
        "previous_account_files": lock(
            [CODE_ROOT / "app/auth.py", CODE_ROOT / "app/account_admin.py", CODE_ROOT / "app/cli.py"]
        ),
        "shared_vendor_files": lock(Path(argon2.__file__).parent.glob("*.py")),
    }
    (root / "baseline.json").write_text(json.dumps(baseline, indent=2), encoding="utf-8")
    os.environ["TECHDOCS_FORMAL_ROOT"] = str(reference)
    return ensure_context()
