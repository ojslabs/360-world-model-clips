"""Create an isolated Python environment, install pinned tools and verify the VAD model."""
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def main():
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Run bootstrap.py with Python 3.12: python3.12 bootstrap.py")
    environment = ROOT / ".venv"
    python = environment / "bin" / "python"
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run([python, "-m", "pip", "install", "-r", ROOT / "requirements.txt"], check=True)
    subprocess.run([python, ROOT / "crowd_audio.py", "--setup-model"], check=True)
    subprocess.run([python, ROOT / "build_ui.py"], check=True)
    subprocess.run([python, ROOT / "doctor.py", "--model"], check=True)
    print("Ready. Run .venv/bin/python server.py and open http://127.0.0.1:8476")


if __name__ == "__main__":
    main()
