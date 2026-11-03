"""Create a NEW external synthetic demo; never reuse or migrate existing data."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from app.db_policy import safe_target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--models-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'models')
    parser.add_argument('--port', type=int, default=8388)
    args = parser.parse_args()
    root = args.data_dir.absolute()
    if not 1024 <= args.port <= 65535:
        parser.error('Port must be between 1024 and 65535')
    if root.exists():
        parser.error('Data directory already exists; choose a new path. Nothing was changed.')
    os.environ['TECHDOCS_DATA_ROOT'] = str(root)
    database = safe_target(root / 'ui.sqlite3')
    root.mkdir(parents=True, exist_ok=False)
    code = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH': str(code), 'PYTHONDONTWRITEBYTECODE': '1'}
    subprocess.run([sys.executable, '-B', '-m', 'tools.ui_fixture', '--database', str(database),
                    '--credentials', str(root / 'credentials.json')], cwd=code, env=env, check=True)
    (root / 'DEMO_ONLY').write_text('synthetic-demo-only', encoding='utf-8')
    config = {'data_root': str(root), 'database': str(database),
              'models': str(args.models_dir.resolve()), 'port': args.port, 'worker': True}
    (root / 'demo-config.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
    print('Ready. Credentials stay local in:', root / 'credentials.json')
    print('Start with: python -B -m tools.demo_launch --config "' + str(root / 'demo-config.json') + '"')


if __name__ == '__main__':
    main()
