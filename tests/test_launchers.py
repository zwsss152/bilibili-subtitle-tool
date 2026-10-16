"""Exercise the Windows command parser, including ZIP-style LF checkouts."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from src.paths import PROJECT_ROOT


@unittest.skipUnless(os.name == 'nt', 'Windows launchers')
class LauncherTests(unittest.TestCase):
    def test_missing_environment_explains_setup_and_returns_failure(self):
        for name in ('launch.cmd', 'diagnostics.cmd'):
            for newline in ('\n', '\r\n'):
                with self.subTest(name=name, newline=repr(newline)), tempfile.TemporaryDirectory() as folder:
                    root = Path(folder) / '中文路径 with spaces'
                    root.mkdir()
                    script = root / name
                    text = (PROJECT_ROOT / name).read_text(encoding='ascii')
                    script.write_bytes(text.replace('\n', newline).encode('ascii'))
                    result = subprocess.run(['cmd.exe', '/d', '/c', name], cwd=root,
                                            input=b'', capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn(b'setup.cmd', result.stdout)
                    self.assertNotIn(b'not recognized', result.stderr)

    def test_entrypoints_have_ascii_commands_and_delegate_to_shared_scripts(self):
        for name, target in [('启动.bat', 'launch.cmd'), ('批量转写.bat', 'launch.cmd'),
                             ('安装环境.bat', 'setup.cmd'), ('环境诊断.bat', 'diagnostics.cmd')]:
            with self.subTest(name=name):
                script = (PROJECT_ROOT / name).read_text(encoding='ascii')
                self.assertIn(f'call "%~dp0{target}"', script)
