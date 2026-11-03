from dataclasses import dataclass
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    db_path: Path = ROOT / "data" / "techdocs.sqlite3"
    model_dir: Path = ROOT / "models"
    models_enabled: bool = True
    worker_enabled: bool = True
    max_document_bytes: int = 1_000_000
    max_chunks: int = 2500
    origin: str = 'http://127.0.0.1:8377'

    @classmethod
    def from_env(cls):
        return cls(
            db_path=Path(os.getenv("TECHDOCS_DB_PATH", str(ROOT / "data" / "techdocs.sqlite3"))),
            model_dir=Path(os.getenv("TECHDOCS_MODEL_DIR", str(ROOT / "models"))),
            models_enabled=os.getenv("TECHDOCS_MODELS", "1") == "1",
            worker_enabled=os.getenv("TECHDOCS_WORKER", "1") == "1",
            origin=os.getenv('TECHDOCS_ORIGIN', 'http://127.0.0.1:8377'),
        )
