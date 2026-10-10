import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bootstrap import command_hook


@unittest.skipUnless(sys.platform == 'win32', 'Windows shell launch coverage')
class HookLauncherTests(unittest.TestCase):
    def test_hook_command_preserves_stdin_and_supports_spaces(self):
        with tempfile.TemporaryDirectory(prefix='journal launch ') as tmp:
            script = Path(tmp) / 'capture input.py'
            script.write_text('import sys,json\nprint(json.dumps(json.load(sys.stdin)))\n', encoding='utf-8')
            command = command_hook(sys.executable, script, 'start')
            payload = {'cwd': tmp, 'session_id': 'launcher-test'}
            proc = subprocess.run(['cmd.exe', '/d', '/s', '/c', command],
                input=json.dumps(payload).encode(), capture_output=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors='replace'))
            self.assertEqual(json.loads(proc.stdout), payload)

    def test_current_safe_paths_execute_real_handler(self):
        base = Path(__file__).resolve().parents[1]
        # No registry match: the real handler must return {} without creating events.
        with tempfile.TemporaryDirectory() as tmp:
            payload = {'cwd': tmp, 'session_id': 'launcher-test'}
            command = command_hook(sys.executable, base / 'hook.py', 'start')
            proc = subprocess.run(['cmd.exe', '/d', '/s', '/c', command],
                input=json.dumps(payload).encode(), capture_output=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors='replace'))
            self.assertEqual(json.loads(proc.stdout), {})
