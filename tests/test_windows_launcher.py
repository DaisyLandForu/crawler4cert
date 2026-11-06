"""Exercise the real Windows launcher without starting a web server."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "Windows batch launcher")
class WindowsLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="crawler launcher ")
        self.root = Path(self.temp.name)
        shutil.copy2(ROOT / "start_workbench.cmd", self.root / "start_workbench.cmd")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.env = dict(os.environ)
        for key in ("VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONHOME", "PYTHONPATH"):
            self.env.pop(key, None)
        # No Conda or system Python discovery: every successful interpreter is
        # supplied explicitly by the test, while cmd/where remain available.
        self.env["PATH"] = str(self.bin) + os.pathsep + str(Path(os.environ["SystemRoot"]) / "System32")
        self.env["PYTHONPATH"] = str(ROOT)

    def tearDown(self):
        self.temp.cleanup()

    def launch_help(self):
        return subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "start_workbench.cmd", "--help"],
            cwd=self.root, env=self.env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=60,
        )

    def path_python(self):
        (self.bin / "python.cmd").write_text(f'@"{sys.executable}" %*\n@exit /b %errorlevel%\n', encoding="utf-8")

    def test_local_venv_runs_without_conda_or_activation(self):
        venv.EnvBuilder(with_pip=False, system_site_packages=True).create(self.root / ".venv")
        result = self.launch_help()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Python: ".venv\\Scripts\\python.exe"', result.stdout)
        self.assertIn("--no-browser", result.stdout)

    def test_path_python_runs_without_conda(self):
        self.path_python()
        result = self.launch_help()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Python: python", result.stdout)
        self.assertIn("--no-browser", result.stdout)

    def test_missing_dependencies_show_setup_instead_of_starting(self):
        venv.EnvBuilder(with_pip=False, system_site_packages=False).create(self.root / ".venv")
        result = self.launch_help()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("No usable Python found", result.stdout)
        self.assertIn("pip install -r requirements.txt", result.stdout)


if __name__ == "__main__":
    unittest.main()
