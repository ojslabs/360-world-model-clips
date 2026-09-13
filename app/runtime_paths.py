"""Keep writable media separate from installed application tools."""
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def data_dir(root=PROJECT_ROOT):
    return Path(os.environ.get("FOOTBALL_DATA_DIR") or Path(root) / ".runtime").expanduser().resolve()
