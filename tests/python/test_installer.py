"""Exercise the Bash installer with real archives and local Docker/curl doubles."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from app.runtime_paths import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts/install.sh"
OWNER = "io.ojslabs.world-model-clips"
LOCATION = OWNER + ".install-dir"
STUB = r'''
import json,os,shutil,sys
from pathlib import Path
tool=Path(sys.argv[0]).name; args=sys.argv[1:]
state_path=Path(os.environ['INSTALLER_TEST_STATE'])
state=json.loads(state_path.read_text()) if state_path.exists() else {'container':{},'image':{},'volume':{},'calls':[]}
state['calls'].append([tool,*args])
def save(): state_path.write_text(json.dumps(state))
def fail(code=1): save();raise SystemExit(code)
def labels(): return dict(args[i+1].split('=',1) for i,v in enumerate(args) if v=='--label')
if tool=='sleep': raise SystemExit(0)
if tool=='curl':
    if '--output' in args:
        target=Path(args[args.index('--output')+1])
        if os.environ.get('INSTALLER_FAIL_DOWNLOAD'):
            target.write_bytes(b'partial download');fail(22)
        shutil.copyfile(os.environ['INSTALLER_TEST_ARCHIVE'],target)
    elif os.environ.get('INSTALLER_FAIL_HEALTH'): fail(7)
    elif args[-1].endswith('/healthz'): print('{"ok":true}')
    elif args[-1].endswith('/login'): print('<title>Sign in to World Model Clips</title>')
    else: fail(22)
    save();raise SystemExit(0)
if args==['info']:
    if os.environ.get('INSTALLER_NO_DAEMON'): fail()
elif len(args)>1 and args[1]=='inspect':
    kind=args[0]; name=args[-1]
    obj=state[kind].get(name)
    if obj is None: fail()
    if '--format' not in args: print(json.dumps([obj]))
    else:
        form=args[args.index('--format')+1]
        if 'State.Running' in form: print(str(obj['running']).lower())
        elif 'HostConfig.PortBindings' in form: print(obj['port'])
        elif 'install-dir' in form: print(obj['labels'].get('io.ojslabs.world-model-clips.install-dir',''))
        else: print(obj['labels'].get('io.ojslabs.world-model-clips',''))
elif args[0]=='build':
    if os.environ.get('INSTALLER_FAIL_BUILD'): fail()
    state['image'][args[args.index('--tag')+1]]={'labels':labels()}
elif args[:2]==['volume','create']:
    state['volume'].setdefault(args[-1],{'labels':labels(),'sentinel':'unchanged'})
    print(args[-1])
elif args[0]=='run':
    if os.environ.get('INSTALLER_FAIL_PORT'): fail(125)
    name=args[args.index('--name')+1]
    config=Path(args[args.index('--env-file')+1]).read_text()
    state['container'][name]={'id':'fixed-test-container','labels':labels(),'running':True,
        'port':args[args.index('--publish')+1].removesuffix(':8476'),
        'mount':args[args.index('--mount')+1],
        'environment':dict(line.split('=',1) for line in config.splitlines())}
    print('fixed-test-container')
elif args[0]=='start': state['container'][args[-1]]['running']=True;print(args[-1])
elif args[0]=='logs': print('Fixture container log; rk_should_be_redacted')
else: print('Unexpected Docker invocation: '+repr(args),file=sys.stderr);fail(99)
save()
'''


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.install = self.root / "install with spaces"
        self.bin = self.root / "tools"
        self.bin.mkdir()
        for tool in ("bash", "tar", "gzip", "uname", "mktemp", "mkdir", "rmdir", "chmod", "cat", "sed", "tr", "od", "mv", "rm"):
            executable = shutil.which(tool)
            self.assertIsNotNone(executable, tool)
            (self.bin / tool).symlink_to(executable)
        for tool in ("docker", "curl", "sleep"):
            path = self.bin / tool
            path.write_text(f"#!{sys.executable}\n" + STUB)
            path.chmod(0o755)
        self.state_file = self.root / "state.json"
        self.archive = self.root / "source.tar.gz"
        self.make_archive()
        self.environment = {**os.environ, "PATH": str(self.bin), "INSTALLER_TEST_STATE": str(self.state_file),
                            "INSTALLER_TEST_ARCHIVE": str(self.archive),
                            "FAL_KEY": "owner-fal-must-not-be-forwarded", "REACTOR_API_KEY": "rk_owner_must_not_be_forwarded"}

    def make_archive(self, extra=None, ref="main"):
        prefix = f"360-world-model-clips-{ref}"
        files = {"Dockerfile": b"FROM scratch\n", "bootstrap.py": b"", "server.py": b"",
                 "requirements-linux.lock": b"", "app/server.py": b"", "config/orbit_preset.json": b"{}",
                 "ui/sections/00_editor.html": b"<html></html>"}
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, content in files.items():
                info = tarfile.TarInfo(f"{prefix}/{name}")
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
            if extra:
                archive.addfile(extra, io.BytesIO(b"x") if extra.isreg() else None)

    def run_installer(self, *args, environment=None):
        return subprocess.run([str(self.bin / "bash"), SCRIPT, "--install-dir", self.install, "--no-open", *args],
                              cwd=self.root, env={**self.environment, **(environment or {})},
                              capture_output=True, text=True, timeout=15)

    def state(self):
        return json.loads(self.state_file.read_text()) if self.state_file.exists() else {"calls": [], "container": {}, "image": {}, "volume": {}}

    def assert_not_started(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("World Model Clips is ready:", result.stdout)
        self.assertFalse(self.state()["container"])

    def test_first_install_uses_complete_archive_private_local_settings_and_no_owner_keys(self):
        result = self.run_installer("--port", "18642", "--name", "test-clips")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("World Model Clips is ready: http://localhost:18642", result.stdout)
        config = self.install / "config.env"
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        container = self.state()["container"]["test-clips"]
        self.assertEqual(container["port"], "127.0.0.1:18642")
        self.assertEqual(container["mount"], "source=test-clips-data,target=/data")
        self.assertEqual(set(container["environment"]), {"DEMO_PASSWORD", "PUBLIC_ORIGIN"})
        self.assertRegex(container["environment"]["DEMO_PASSWORD"], "^[a-f0-9]{48}$")
        self.assertEqual(container["labels"][LOCATION], str(self.install))
        self.assertTrue((self.install / "source/config/orbit_preset.json").is_file())
        self.assertFalse((self.install / ".install-lock").exists())
        self.assertNotIn("owner_must_not", config.read_text() + result.stdout + result.stderr)

    def test_restart_reuses_same_container_source_config_and_persistent_volume(self):
        first = self.run_installer("--port", "18643")
        self.assertEqual(first.returncode, 0, first.stderr)
        config = (self.install / "config.env").read_bytes()
        source = self.install / "source/server.py"
        source.write_text("user edit must remain\n")
        state = self.state()
        state["container"]["world-model-clips"]["running"] = False
        state["calls"] = []
        self.state_file.write_text(json.dumps(state))
        second = self.run_installer()
        self.assertEqual(second.returncode, 0, second.stderr)
        state = self.state()
        self.assertEqual(state["container"]["world-model-clips"]["id"], "fixed-test-container")
        self.assertEqual(state["volume"]["world-model-clips-data"]["sentinel"], "unchanged")
        self.assertEqual((self.install / "config.env").read_bytes(), config)
        self.assertEqual(source.read_text(), "user edit must remain\n")
        self.assertIn(["docker", "start", "world-model-clips"], state["calls"])
        self.assertFalse(any(call[0] == "curl" and "--output" in call or call[:2] == ["docker", "build"] for call in state["calls"]))

    def test_running_owned_container_is_not_restarted(self):
        self.assertEqual(self.run_installer().returncode, 0)
        state = self.state(); state["calls"] = []; self.state_file.write_text(json.dumps(state))
        self.assertEqual(self.run_installer().returncode, 0)
        self.assertFalse(any(call[:2] in (["docker", "start"], ["docker", "run"], ["docker", "build"]) for call in self.state()["calls"]))

    def test_pinned_commit_archive_is_downloaded_and_recorded_without_git(self):
        reference = "a1" * 20
        self.make_archive(ref=reference)
        result = self.run_installer("--ref", reference)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.install / "source-ref").read_text().strip(), reference)
        downloads = [entry for entry in self.state()["calls"] if entry[0] == "curl" and "--output" in entry]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0][-1], "https://codeload.github.com/ojslabs/360-world-model-clips/tar.gz/" + reference)

    def test_unowned_volume_is_preserved_without_building_or_starting(self):
        self.state_file.write_text(json.dumps({"container": {}, "image": {}, "volume": {
            "world-model-clips-data": {"labels": {}, "sentinel": "prior media"}}, "calls": []}))
        result = self.run_installer()
        self.assert_not_started(result)
        self.assertIn("Volume world-model-clips-data", result.stderr)
        self.assertEqual(self.state()["volume"]["world-model-clips-data"]["sentinel"], "prior media")
        self.assertFalse(any(call[:2] == ["docker", "build"] for call in self.state()["calls"]))

    def test_missing_docker_and_unavailable_daemon_fail_before_creating_installation(self):
        (self.bin / "docker").rename(self.bin / "saved-docker")
        result = self.run_installer()
        self.assert_not_started(result)
        self.assertIn("Missing docker", result.stderr)
        self.assertFalse(self.install.exists())
        (self.bin / "saved-docker").rename(self.bin / "docker")
        result = self.run_installer(environment={"INSTALLER_NO_DAEMON": "1"})
        self.assert_not_started(result)
        self.assertIn("Docker is not running", result.stderr)
        self.assertFalse(self.install.exists())

    def test_unowned_directory_or_docker_name_collision_is_not_replaced(self):
        self.install.mkdir(); marker = self.install / "my-file"; marker.write_text("mine")
        result = self.run_installer()
        self.assert_not_started(result)
        self.assertEqual(marker.read_text(), "mine")
        shutil.rmtree(self.install)
        state = self.state(); state["container"]["world-model-clips"] = {"labels": {}, "id": "not-ours"}
        self.state_file.write_text(json.dumps(state))
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not owned", result.stderr)
        self.assertEqual(self.state()["container"]["world-model-clips"]["id"], "not-ours")
        self.assertFalse(any(call[:2] == ["docker", "build"] for call in self.state()["calls"]))

    def test_failed_download_build_port_and_health_never_report_ready(self):
        for failure in ("DOWNLOAD", "BUILD", "PORT", "HEALTH"):
            with self.subTest(failure=failure):
                if self.install.exists(): shutil.rmtree(self.install)
                self.state_file.unlink(missing_ok=True)
                result = self.run_installer(environment={f"INSTALLER_FAIL_{failure}": "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("World Model Clips is ready:", result.stdout)
                self.assertNotIn("Access code:", result.stdout)
                self.assertNotIn("rk_should_be_redacted", result.stderr)
                if failure == "DOWNLOAD":
                    self.assertFalse((self.install / "source").exists())
                    self.assertFalse(any(call[:2] == ["docker", "build"] for call in self.state()["calls"]))

    def test_archive_parent_paths_and_links_are_rejected_before_build(self):
        for kind in ("traversal", "symlink"):
            with self.subTest(kind=kind):
                if self.install.exists(): shutil.rmtree(self.install)
                self.state_file.unlink(missing_ok=True)
                extra = tarfile.TarInfo("360-world-model-clips-main/../../outside" if kind == "traversal" else "360-world-model-clips-main/link")
                if kind == "symlink": extra.type = tarfile.SYMTYPE; extra.linkname = "/tmp"
                else: extra.size = 1
                self.make_archive(extra)
                result = self.run_installer()
                self.assert_not_started(result)
                self.assertFalse((self.root / "outside").exists())
                self.assertFalse(any(call[:2] == ["docker", "build"] for call in self.state()["calls"]))

    def test_modified_environment_and_dangling_settings_link_are_not_used_or_overwritten(self):
        self.assertEqual(self.run_installer().returncode, 0)
        config = self.install / "config.env"
        original = config.read_text()
        config.write_text(original + "REACTOR_API_KEY=do-not-pass\n")
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected settings", result.stderr)
        self.assertEqual(config.read_text(), original + "REACTOR_API_KEY=do-not-pass\n")
        config.unlink(); target = self.root / "outside-settings"; config.symlink_to(target)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())

    def test_help_bad_options_and_truncated_script_have_no_install_side_effects(self):
        self.assertEqual(self.run_installer("--help").returncode, 0)
        self.assertFalse(self.install.exists())
        for args in (("--ref", "../branch"), ("--port", "0"), ("--name", "Bad Name"), ("--unknown",)):
            self.assertNotEqual(self.run_installer(*args).returncode, 0)
            self.assertFalse(self.install.exists())
        truncated = SCRIPT.read_text().rsplit('\nwmc_main "$@"', 1)[0]
        result = subprocess.run([str(self.bin / "bash")], input=truncated, text=True,
                                env=self.environment, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.state()["calls"])
        self.assertFalse(self.install.exists())


if __name__ == "__main__":
    unittest.main()
