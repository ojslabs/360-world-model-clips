"""Create an isolated Python environment, install pinned tools and verify the VAD model."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Start the app after setup succeeds.")
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Run bootstrap.py with Python 3.12: python3.12 bootstrap.py")
    environment = ROOT / ".venv"
    python = environment / "bin" / "python"
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run([python, "-m", "pip", "install", "-r", ROOT / "requirements.txt"], check=True, cwd=ROOT)
    subprocess.run([python, "-m", "app.crowd_audio", "--setup-model"], check=True, cwd=ROOT)
    subprocess.run([python, "-m", "scripts.build_ui"], check=True, cwd=ROOT)
    subprocess.run([python, "-m", "scripts.doctor", "--model"], check=True, cwd=ROOT)
    if args.run:
        print("Setup complete. Starting World Model Clips…", flush=True)
        os.execv(str(python), [str(python), str(ROOT / "server.py")])
        return
    print("Ready. Run .venv/bin/python server.py and open http://127.0.0.1:8476")


if __name__ == "__main__":
    main()
