"""Check downloadable BAT bytes and exercise CMD without installing packages."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUILD = "打包EXE.bat"
LAUNCH = "启动.bat"


class BatchFormatTest(unittest.TestCase):
    def test_scripts_are_ascii_with_windows_newlines(self):
        for name in (BUILD, LAUNCH):
            with self.subTest(script=name):
                data = (ROOT / name).read_bytes()
                data.decode("ascii")
                self.assertTrue(data.endswith(b"\r\n"))
                self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
                self.assertNotIn(b"\r", data.replace(b"\r\n", b""))


@unittest.skipUnless(os.name == "nt", "Requires Windows CMD")
class BatchExecutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "中文 project with spaces"
        self.project.mkdir()
        self.other = Path(self.tmp.name) / "unrelated directory"
        self.other.mkdir()
        for name in (BUILD, LAUNCH):
            shutil.copyfile(str(ROOT / name), str(self.project / name))
        (self.project / "requirements.txt").write_text("", encoding="ascii")
        stub = '''import json, os, sys
from pathlib import Path
module = Path(__file__).stem
with open(os.environ["BAT_TEST_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps([module, sys.argv[1:], os.getcwd()]) + "\\n")
if module == "pip":
    key = "SHOW_EXIT" if sys.argv[1] == "show" else "INSTALL_EXIT"
elif module == "PyInstaller":
    key = "BUILD_EXIT"
    if os.environ.get("CREATE_EXE", "1") == "1":
        path = Path("dist/DiskCleaner/DiskCleaner.exe")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
else:
    key = "MAIN_EXIT"
sys.exit(int(os.environ.get(key, "0")))
'''
        for name in ("pip.py", "PyInstaller.py", "main.py"):
            (self.project / name).write_text(stub, encoding="ascii")
        self.log = Path(self.tmp.name) / "calls.jsonl"
        self.env = os.environ.copy()
        self.env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + self.env.get("PATH", "")
        self.env["BAT_TEST_LOG"] = str(self.log)
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        for key in ("SHOW_EXIT", "INSTALL_EXIT", "BUILD_EXIT", "MAIN_EXIT", "CREATE_EXE"):
            self.env.pop(key, None)

    def run_script(self, name, **env):
        self.env.update(env)
        command = '"{}" /d /s /c ""{}" --no-pause"'.format(
            os.environ.get("COMSPEC", "cmd.exe"), self.project / name)
        result = subprocess.run(
            command,
            cwd=str(self.other), env=self.env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=30,
        )
        calls = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []
        for call in calls:
            self.assertEqual(Path(call[2]), self.project)
        return result, calls

    def test_build_from_another_directory(self):
        result, calls = self.run_script(BUILD)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([call[0] for call in calls], ["pip", "PyInstaller"])
        self.assertEqual(calls[0][1], ["install", "-r", "requirements.txt", "pyinstaller"])
        self.assertIn(b"[OK]", result.stdout)

    def test_failed_install_does_not_build(self):
        result, calls = self.run_script(BUILD, INSTALL_EXIT="1")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual([call[0] for call in calls], ["pip"])
        self.assertNotIn(b"[OK]", result.stdout)

    def test_failed_build_does_not_report_success(self):
        result, calls = self.run_script(BUILD, BUILD_EXIT="1")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn(b"[OK]", result.stdout)

    def test_missing_exe_is_a_build_failure(self):
        result, calls = self.run_script(BUILD, CREATE_EXE="0")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn(b"[OK]", result.stdout)

    def test_missing_source_fails_before_installing(self):
        (self.project / "main.py").unlink()
        result, calls = self.run_script(BUILD)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(calls, [])

    def test_launch_from_another_directory(self):
        result, calls = self.run_script(LAUNCH)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([call[0] for call in calls], ["pip", "main"])

    def test_launch_installs_missing_dependency(self):
        result, calls = self.run_script(LAUNCH, SHOW_EXIT="1")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([call[0] for call in calls], ["pip", "pip", "main"])
        self.assertEqual(calls[1][1], ["install", "-r", "requirements.txt"])

    def test_launch_stops_after_failed_install(self):
        result, calls = self.run_script(LAUNCH, SHOW_EXIT="1", INSTALL_EXIT="1")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual([call[0] for call in calls], ["pip", "pip"])

    def test_app_failure_returns_nonzero(self):
        result, calls = self.run_script(LAUNCH, MAIN_EXIT="1")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(b"[ERROR]", result.stdout)


if __name__ == "__main__":
    unittest.main()
