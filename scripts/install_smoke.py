"""Exercise the published Bash installer on a fresh Docker-capable machine."""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
from urllib.request import urlopen
import uuid

from scripts.container_smoke import verify_http
from app.runtime_paths import PROJECT_ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True, help="The public commit being checked, as a full SHA.")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.ref):
        parser.error("--ref must be a full commit SHA")
    docker_path = shutil.which("docker")
    if not docker_path:
        parser.error("Docker must be installed and running")
    repo = PROJECT_ROOT
    name = "clips-install-smoke-" + uuid.uuid4().hex[:12]
    volume = name + "-data"
    image = name + ":installed"

    def docker(*arguments):
        return subprocess.run([docker_path, *arguments], check=True, capture_output=True,
                              text=True, timeout=120).stdout.strip()

    with tempfile.TemporaryDirectory(prefix="clips-fresh-install-") as temporary:
        scratch = Path(temporary)
        restricted_bin = scratch / "bin"
        restricted_bin.mkdir()
        # A runner has many preinstalled languages. The installer cannot use them.
        for tool in ("bash", "curl", "tar", "gzip", "docker", "uname", "mktemp", "mkdir", "rmdir",
                     "chmod", "cat", "sed", "tr", "od", "mv", "rm", "sleep"):
            command = shutil.which(tool)
            if not command:
                raise RuntimeError("Missing documented host tool: " + tool)
            (restricted_bin / tool).symlink_to(command)
        for tool in ("python", "python3", "python3.12", "node", "ffmpeg", "ffprobe", "git"):
            assert shutil.which(tool, path=str(restricted_bin)) is None
        install_dir = scratch / "new user" / "world-model-clips"
        private_home = scratch / "home"
        private_home.mkdir()
        # Preserve installed Docker CLI plugins, never the runner's account config.
        plugins = Path.home() / ".docker" / "cli-plugins"
        if plugins.is_dir():
            (private_home / ".docker").mkdir()
            (private_home / ".docker" / "cli-plugins").symlink_to(plugins)
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("FAL", "REACTOR", "FOOTBALL", "DEMO"))}
        environment.update(HOME=str(private_home), PATH=str(restricted_bin),
                           DOCKER_CONFIG=str(private_home / ".docker"))
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        url = f"https://github.com/ojslabs/360-world-model-clips/raw/{args.ref}/scripts/install.sh"
        with urlopen(url, timeout=30) as response:
            script = response.read(2 * 1024 * 1024)
        assert script == (repo / "scripts/install.sh").read_bytes(), "Published installer differs from tested source."
        command = [str(restricted_bin / "bash"), "-s", "--", "--ref", args.ref,
                   "--install-dir", str(install_dir), "--name", name, "--port", str(port), "--no-open"]

        def install():
            result = subprocess.run(command, input=script.decode(), cwd=scratch, env=environment,
                                    capture_output=True, text=True, timeout=1200)
            if result.returncode:
                output = result.stdout + result.stderr
                config = install_dir / "config.env"
                if config.is_file():
                    for line in config.read_text().splitlines():
                        if line.startswith("DEMO_PASSWORD="):
                            output = output.replace(line.partition("=")[2], "[local passcode]")
                raise AssertionError("Installer failed:\n" + output[-12000:])

        try:
            install()
            info = json.loads(docker("inspect", name))[0]
            identity = info["Id"]
            assert info["State"]["Running"]
            assert info["HostConfig"]["PortBindings"]["8476/tcp"] == [
                {"HostIp": "127.0.0.1", "HostPort": str(port)}]
            assert docker("exec", name, "id", "-u") != "0"
            config = install_dir / "config.env"
            assert config.stat().st_mode & 0o777 == 0o600
            values = dict(line.split("=", 1) for line in config.read_text().splitlines() if "=" in line)
            assert set(values) == {"DEMO_PASSWORD", "PUBLIC_ORIGIN"}
            assert values["PUBLIC_ORIGIN"] == f"http://localhost:{port}"
            verify_http(port, values["DEMO_PASSWORD"])
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/login", headers={"Host": f"localhost:{port}"})
            response = connection.getresponse()
            assert response.status == 200 and b"password" in response.read()
            connection.close()
            installed_source = install_dir / "source"
            for filename in ("bootstrap.py", "app/server.py", "config/orbit_preset.json",
                             "ui/assets/reactor-live.js", "tests/python/test_end_to_end.py"):
                assert (installed_source / filename).read_bytes() == (repo / filename).read_bytes(), filename
            configured = dict(value.split("=", 1) for value in info["Config"]["Env"] if "=" in value)
            assert not configured.get("FAL_KEY") and not configured.get("REACTOR_API_KEY")
            print("Fresh installer passed: downloaded source, runtime tools, non-root startup, loopback binding, protected editor and blank keys.", flush=True)
            docker("exec", name, "python", "-m", "scripts.doctor", "--model")
            docker("exec", name, "python", "-m", "unittest", "-v", "tests.python.test_end_to_end")
            print("Installed image passed the offline HTTP import-to-download test with real FFmpeg.", flush=True)
            docker("exec", name, "python", "-c",
                   "from pathlib import Path; Path('/data/install-sentinel').write_text('keep this edit')")
            original_config = config.read_bytes()
            docker("stop", name)
            install()
            assert json.loads(docker("inspect", name))[0]["Id"] == identity
            assert config.read_bytes() == original_config
            assert docker("exec", name, "cat", "/data/install-sentinel") == "keep this edit"
            verify_http(port, values["DEMO_PASSWORD"])
            print("Restart passed: same container, private access code and persistent data.", flush=True)
        finally:
            # These names are unique resources created only by this smoke test.
            for arguments in (("rm", "--force", name), ("volume", "rm", volume), ("image", "rm", image)):
                subprocess.run([docker_path, *arguments], capture_output=True, check=False, timeout=120)


if __name__ == "__main__":
    main()
