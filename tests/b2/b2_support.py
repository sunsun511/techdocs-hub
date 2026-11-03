from pathlib import Path
import os
from tools.test_context import ensure_context

ensure_context()
ROOT = Path(os.environ["TECHDOCS_ACCEPTANCE_ROOT"])
FORMAL_ROOT = Path(os.environ["TECHDOCS_FORMAL_ROOT"])
CODE_ROOT = Path(__file__).resolve().parents[2]
