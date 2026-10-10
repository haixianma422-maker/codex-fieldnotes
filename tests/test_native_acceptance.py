import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from journal_core import digest
from native_test import verify


class NativeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.events = self.base / 'events/demo-project'
        self.receipts = self.base / 'state/receipts/demo-project'
        self.project = self.base / 'demo-project'
        for p in (self.events, self.receipts, self.project / 'tests'):
            p.mkdir(parents=True)
        (self.project / 'title_tools.py').write_text('def normalize_title(x): return x\n')
        (self.project / 'tests/test_title_tools.py').write_text('')
        self.note = self.base / 'note.md'
        self.note.write_text('actual published note')
        self.report = {'native_session': 'native-123', 'bounded_redacted_output': 'tail without thread.started',
                       'exit_code': 0, 'timed_out': False}
        self.event = {'session_id': 'native-123', 'event_id': 'event-1', 'tool_use_id': 'tool-1',
                      'classification': 'test_result', 'exit_code': 0, 'excerpt': 'Ran 6 tests in 0.01s\nOK'}
        self.receipt = {'status': 'published', 'event_ids': ['event-1'], 'path': str(self.note),
                        'content_hash': digest(self.note.read_bytes())}

    def audit(self):
        (self.events / 'event.json').write_text(json.dumps(self.event))
        (self.receipts / 'receipt.json').write_text(json.dumps(self.receipt))
        return verify(dict(self.report), self.base)

    def test_streamed_identity_survives_output_truncation_and_nested_tests(self):
        self.assertEqual(self.audit()['status'], 'passed')

    def test_manual_runner_does_not_count_as_hook(self):
        del self.event['tool_use_id']
        result = self.audit()
        self.assertEqual(result['verification']['native_hook_events'], 0)
        self.assertNotEqual(result['status'], 'passed')

    def test_changed_published_note_invalidates_receipt(self):
        self.note.write_text('edited after publication')
        self.assertFalse(self.audit()['verification']['linked_publication_receipt'])

    def test_failed_tests_cannot_pass_with_successful_cli_exit(self):
        self.event.update(exit_code=1, excerpt='Ran 6 tests\nFAILED (failures=1)')
        self.assertNotEqual(self.audit()['status'], 'passed')

    def test_timeout_cannot_pass_even_with_existing_artifacts(self):
        self.report['timed_out'] = True
        self.assertNotEqual(self.audit()['status'], 'passed')

    def test_stop_exercise_requires_real_retry_marker(self):
        self.report['exercise_stop'] = True
        self.assertNotEqual(self.audit()['status'], 'passed')
        retries = self.base / 'state/retries'
        retries.mkdir(parents=True)
        (retries / 'retry.json').write_text(json.dumps({'events': ['event-1']}))
        self.assertEqual(self.audit()['status'], 'passed')

    def test_unrelated_retry_does_not_satisfy_stop_exercise(self):
        self.report['exercise_stop'] = True
        retries = self.base / 'state/retries'
        retries.mkdir(parents=True)
        (retries / 'retry.json').write_text(json.dumps({'events': ['old-event']}))
        self.assertNotEqual(self.audit()['status'], 'passed')

    def test_benign_sandbox_text_does_not_claim_runtime_blocker(self):
        self.report['bounded_redacted_output'] = 'Documentation says use workspace-write sandbox'
        self.event['exit_code'] = None
        self.assertEqual(self.audit()['status'], 'failed')

    def test_actual_sandbox_setup_failure_is_blocked(self):
        self.report['bounded_redacted_output'] = 'helper_unknown_error: setup refresh had errors'
        self.event['exit_code'] = None
        self.assertEqual(self.audit()['status'], 'blocked')


if __name__ == '__main__':
    unittest.main()
