"""Bootstrap setup, validation and process handoff without installing dependencies."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch

import bootstrap


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = (Path(temporary.name) / "checkout").resolve()
        self.python = self.root / ".venv/bin/python"
        replacement = patch.object(bootstrap, "ROOT", self.root)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.commands = [
            [self.python, "-m", "pip", "install", "-r", self.root / "requirements.txt"],
            [self.python, "-m", "app.crowd_audio", "--setup-model"],
            [self.python, "-m", "scripts.build_ui"],
            [self.python, "-m", "scripts.doctor", "--model"],
        ]

    def test_default_finishes_setup_without_starting_a_server(self):
        with patch.object(bootstrap.sys, "version_info", (3, 12, 0)), \
                patch.object(bootstrap.venv, "EnvBuilder") as builder, \
                patch.object(bootstrap.subprocess, "run") as run, \
                patch.object(bootstrap.os, "execv") as execute, redirect_stdout(io.StringIO()) as output:
            bootstrap.main([])
        builder.assert_called_once_with(with_pip=True)
        builder.return_value.create.assert_called_once_with(self.root / ".venv")
        self.assertEqual(run.call_args_list, [call(command, check=True, cwd=self.root) for command in self.commands])
        execute.assert_not_called()
        self.assertIn("Ready. Run .venv/bin/python server.py", output.getvalue())

    def test_run_reuses_existing_environment_and_executes_absolute_server_path(self):
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        with patch.object(bootstrap.sys, "version_info", (3, 12, 0)), \
                patch.object(bootstrap.venv, "EnvBuilder") as builder, \
                patch.object(bootstrap.subprocess, "run") as run, \
                patch.object(bootstrap.os, "execv") as execute, redirect_stdout(io.StringIO()):
            bootstrap.main(["--run"])
        builder.assert_not_called()
        self.assertEqual(run.call_args_list, [call(command, check=True, cwd=self.root) for command in self.commands])
        execute.assert_called_once_with(str(self.python), [str(self.python), str(self.root / "server.py")])

    def test_help_invalid_arguments_and_wrong_python_do_not_start_setup(self):
        for args, version, code in ((["--help"], (3, 11, 0), 0),
                                    (["--unknown"], (3, 12, 0), 2),
                                    (["--run"], (3, 13, 0), None)):
            with self.subTest(args=args, version=version), \
                    patch.object(bootstrap.sys, "version_info", version), \
                    patch.object(bootstrap.venv, "EnvBuilder") as builder, \
                    patch.object(bootstrap.subprocess, "run") as run, \
                    patch.object(bootstrap.os, "execv") as execute, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as stopped:
                bootstrap.main(args)
            if code is None:
                self.assertIn("Python 3.12", str(stopped.exception))
            else:
                self.assertEqual(stopped.exception.code, code)
            builder.assert_not_called()
            run.assert_not_called()
            execute.assert_not_called()
            self.assertFalse(self.root.exists())

    def test_any_setup_failure_prevents_launch_and_later_setup_steps(self):
        for failure_at in range(len(self.commands)):
            failure = subprocess.CalledProcessError(7, self.commands[failure_at])
            with self.subTest(failure_at=failure_at), \
                    patch.object(bootstrap.sys, "version_info", (3, 12, 0)), \
                    patch.object(bootstrap.venv, "EnvBuilder"), \
                    patch.object(bootstrap.subprocess, "run", side_effect=[None] * failure_at + [failure]) as run, \
                    patch.object(bootstrap.os, "execv") as execute, \
                    self.assertRaises(subprocess.CalledProcessError):
                bootstrap.main(["--run"])
            self.assertEqual(run.call_count, failure_at + 1)
            execute.assert_not_called()

    def test_real_exec_preserves_pid_environment_and_server_exit_from_another_directory(self):
        self.python.parent.mkdir(parents=True)
        self.python.symlink_to(sys.executable)
        report = self.root / "handoff.json"
        (self.root / "server.py").write_text(
            "import json,os,sys\nfrom pathlib import Path\n"
            "Path(__file__).with_name('handoff.json').write_text(json.dumps({"
            "'pid':os.getpid(),'cwd':os.getcwd(),'value':os.environ['BOOTSTRAP_TEST'],"
            "'data':os.environ['FOOTBALL_DATA_DIR'],'script':sys.argv[0]}))\n"
            "raise SystemExit(17)\n")
        checkout = Path(bootstrap.__file__).resolve().parent
        launcher = self.root / "launcher.py"
        # Setup is mocked only in this child; the final exec and server are real.
        launcher.write_text(
            "import importlib.util\nfrom pathlib import Path\n"
            f"spec=importlib.util.spec_from_file_location('bootstrap',{str(checkout / 'bootstrap.py')!r})\n"
            "bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)\n"
            f"bootstrap.ROOT=Path({str(self.root)!r})\n"
            "bootstrap.sys.version_info=(3,12,0)\nbootstrap.subprocess.run=lambda *args,**kwargs:None\n"
            "bootstrap.main(['--run'])\n")
        environment = {**os.environ, "BOOTSTRAP_TEST": "preserved", "FOOTBALL_DATA_DIR": str(self.root / "data")}
        child = subprocess.Popen([sys.executable, launcher], cwd=self.root.parent,
                                 env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            _, errors = child.communicate(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        self.assertEqual(child.returncode, 17, errors.decode())
        self.assertEqual(json.loads(report.read_text()), {
            "pid": child.pid, "cwd": str(self.root.parent), "value": "preserved",
            "data": str(self.root / "data"), "script": str(self.root / "server.py"),
        })


if __name__ == "__main__":
    unittest.main()
