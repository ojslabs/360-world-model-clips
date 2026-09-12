"""Keep writable media separate from installed application tools."""
import os
from pathlib import Path


def data_dir(root):
    return Path(os.environ.get("FOOTBALL_DATA_DIR") or Path(root) / ".runtime").expanduser().resolve()
