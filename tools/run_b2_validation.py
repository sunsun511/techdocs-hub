"""Portable acceptance runner with private raw output and redacted artifacts."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from .test_context import CODE_ROOT, ensure_context


def secret_values(root):
    result = set()
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [
            n
            for n in dirs
            if not (Path(directory) / n).is_symlink() and not (Path(directory) / n).is_junction()
        ]
        for name in names:
            if name.startswith("security-secrets") and name.endswith(".json"):
                result.update(json.loads((Path(directory) / name).read_text(encoding="utf-8")))
    return sorted(result, key=len, reverse=True)


def scrub(value, secrets):
    for secret in secrets:
        value = value.replace(secret, "[REDACTED]")
    return value


def run(kind, out, run_id):
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    context = ensure_context(out)
    run_dir = out / run_id
    run_dir.mkdir(exist_ok=False)
    private = context / "private_data" / run_id
    private.mkdir(parents=True, exist_ok=False)
    locked = {}
    for folder in ("app", "tests", "tools", "fixtures"):
        for path in (CODE_ROOT / folder).rglob("*"):
            if path.is_file() and path.suffix in (".py", ".json") and "__pycache__" not in path.parts:
                locked[str(path.relative_to(CODE_ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    (run_dir / "source-lock.json").write_text(json.dumps(locked, indent=2), encoding="utf-8")
    files = {
        "all": ["tests"],
        "workspace": ["tests/b2/test_workspace.py"],
        "accounts": ["tests/b2/test_accounts.py"],
        "http": ["tests/b2/test_workspace_http.py"],
        "supplement": ["tests/b2/test_workspace_supplement.py"],
        "engineering": [
            "tests/test_chunking.py",
            "tests/test_lifecycle.py",
            "tests/test_restart.py",
            "tests/test_b1_equivalence.py",
        ],
        "migration": ["tests/b2/test_integration_migration.py"],
        "remediation": ["tests/b2/test_review_fixes.py"],
    }
    env = {
        **os.environ,
        "PYTHONPATH": str(CODE_ROOT),
        "WORKSPACE_RUN_ID": run_id + "-workspace",
        "ACCOUNT_RUN_ID": run_id + "-accounts",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    command = [
        sys.executable,
        "-X",
        "utf8",
        "-B",
        "-m",
        "pytest",
        *files[kind],
        "-q",
        "-x",
        "-p",
        "no:cacheprovider",
        "--basetemp=" + str(private / "cases"),
        "--junitxml=" + str(private / "raw-pytest.xml"),
    ]
    result = subprocess.run(
        command,
        cwd=CODE_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    raw = result.stdout + "\n" + result.stderr
    (private / "raw.log").write_text(raw, encoding="utf-8")
    roots = [
        private,
        *[context / "private_data" / (run_id + suffix) for suffix in ("-accounts", "-workspace")],
    ]
    values = sorted(
        {v for folder in roots if folder.exists() for v in secret_values(folder)}, key=len, reverse=True
    )
    (run_dir / "pytest.log").write_text(scrub(raw, values), encoding="utf-8")
    summary = {
        "exit_code": result.returncode,
        "kind": kind,
        "registered_secrets": len(values),
        "fixture_baseline": json.loads((context / "baseline.json").read_text()).get(
            "fixture_baseline", False
        ),
    }
    xml = private / "raw-pytest.xml"
    if xml.exists():
        (run_dir / "pytest.xml").write_text(scrub(xml.read_text(encoding="utf-8"), values), encoding="utf-8")
        cases = list(ET.parse(xml).getroot().iter("testcase"))
        summary.update(
            tests=len(cases),
            failures=sum(c.find("failure") is not None for c in cases),
            errors=sum(c.find("error") is not None for c in cases),
            skips=sum(c.find("skipped") is not None for c in cases),
        )
    for suffix in ("-accounts", "-workspace"):
        observations = context / (run_id + suffix)
        if observations.exists():
            target = run_dir / ("observations" + suffix)
            target.mkdir()
            for path in observations.glob("*.json"):
                (target / path.name).write_text(
                    scrub(path.read_text(encoding="utf-8"), values), encoding="utf-8"
                )
    shutil.copy2(context / "baseline.json", run_dir / "baseline.json")
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)
    if result.returncode:
        print(scrub(raw, values)[-18000:])
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "kind",
        choices=[
            "all",
            "workspace",
            "accounts",
            "http",
            "supplement",
            "engineering",
            "migration",
            "remediation",
        ],
    )
    parser.add_argument("--evidence-root", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    return run(args.kind, args.evidence_root, args.run_id)["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
