"""Runtime build downloads are hash-pinned and extraction never follows paths."""
import hashlib
import io
import tempfile
from pathlib import Path
import subprocess
import sys
import unittest
import zipfile

from tools import prepare_python_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def archive(self, extra=None):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            for name in ('python.exe', 'python314.dll', 'python314.zip', 'python314._pth', 'LICENSE.txt'):
                archive.writestr(name, b'isolated fixture; never executed')
            if extra:
                for name, data in extra:
                    archive.writestr(name, data)
        value = stream.getvalue()
        return value, hashlib.sha256(value).hexdigest()

    def test_validates_pinned_hash_and_rejects_bad_archive(self):
        value, sha = self.archive()
        self.assertIn('python.exe', runtime.verified_members(value, sha))
        with self.assertRaises(ValueError):
            runtime.verified_members(value)
        with self.assertRaises(ValueError):
            runtime.verified_members(b'')

    def test_archive_paths_and_case_duplicates_are_rejected(self):
        for name in ('../escape.exe', '/escape.exe', 'sub/file.exe', 'evil\\file.exe', 'file.exe:stream', 'PYTHON.EXE'):
            value, sha = self.archive([(name, b'fixture')])
            with self.subTest(name=name), self.assertRaises(ValueError):
                runtime.verified_members(value, sha)

    def test_launcher_runs_user_code_and_imports_sibling_module(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            launcher = base / 'run_user_code.py'
            launcher.write_text(runtime.LAUNCHER, encoding='utf-8')
            project = base / 'My project'
            project.mkdir()
            (project / 'helper.py').write_text('value = 42', encoding='utf-8')
            script = project / 'idea.py'
            script.write_text('import helper\nname = input("Ім’я: ")\nprint(name, helper.value)', encoding='utf-8')
            result = subprocess.run([sys.executable, '-u', '-X', 'utf8', str(launcher), str(script)],
                                    input='MechaHub\n', capture_output=True, text=True, encoding='utf-8', timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('MechaHub 42', result.stdout)
