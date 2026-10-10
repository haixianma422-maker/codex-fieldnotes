"""Regression cases from the Superpowers review; every Vault is temporary."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import test_journal as fixtures
from hook import handle
from journal import run_test
from journal_core import JournalError, digest, lock, put_json, read_json
from knowledge import recall, sync_index
from publisher import publish, lesson_path, render_entry


class ReviewFixTests(unittest.TestCase):
    setUp = fixtures.JournalTests.setUp
    tearDown = fixtures.JournalTests.tearDown
    entry = fixtures.JournalTests.entry
    evidence = fixtures.JournalTests.evidence

    def capture(self, response):
        result = handle('capture', {'cwd': str(self.base / 'alpha'),
            'session_id': 'review', 'turn_id': 'test', 'tool_name': 'Bash',
            'tool_use_id': 'call-' + digest(response)[:10],
            'tool_input': {'command': 'python -m unittest'}, 'tool_response': response}, self.reg)
        context = json.loads(result['hookSpecificOutput']['additionalContext'])
        return read_json(context['evidence_path']), Path(context['evidence_path'])

    def test_stdout_status_text_is_not_passing_evidence(self):
        event, path = self.capture('log Exit code: 0\nFAILED (failures=1)\nProcess exited with code 1')
        e = self.entry()
        e.update(status='verified', event_ids=[event['event_id']])
        e['evidence'] = [self.evidence(path, 'tool_result', 'TEST')]
        e['verification'] = {'result': 'passed', 'evidence_ids': ['TEST'], 'summary': 'Passing claim'}
        self.assertEqual(publish(e, self.reg)['status'], 'needs_review')

    def test_wrapper_metadata_wins_over_stdout_status(self):
        event, _ = self.capture('Chunk ID: abc\nWall time: 1 seconds\nProcess exited with code 1\nFinal output:\nExit code: 0')
        self.assertEqual(event['exit_code'], 1)

    def test_only_stdout_status_stays_unknown(self):
        event, _ = self.capture('Exit code: 0')
        self.assertIsNone(event['exit_code'])

    def test_boolean_exit_code_is_not_success(self):
        event, _ = self.capture({'exit_code': False, 'output': 'OK'})
        self.assertIsNone(event['exit_code'])

    def test_edited_foreign_note_does_not_block_publication(self):
        first = publish(self.entry(), self.reg)
        Path(first['path']).write_text('User edited alpha', encoding='utf-8')
        result = publish(self.entry(pid='beta'), self.reg)
        self.assertEqual(result['status'], 'published')
        self.assertEqual(Path(first['path']).read_text(encoding='utf-8'), 'User edited alpha')

    def test_interrupted_new_entry_is_not_recalled(self):
        with patch('publisher.rebuild', side_effect=RuntimeError('interruption')):
            with self.assertRaises(RuntimeError):
                publish(self.entry(), self.reg)
        with lock(self.base):
            sync_index(self.reg)
        self.assertEqual(recall(self.reg, 'alpha', '本地')['cards'], [])
        self.assertEqual(publish(self.entry(), self.reg)['status'], 'published')
        self.assertTrue(recall(self.reg, 'alpha', '本地')['cards'])

    def test_receipt_failure_excludes_record_and_retry_recovers(self):
        import publisher
        actual = publisher.put_json
        def interrupt(path, value):
            if Path(path).is_relative_to(self.base / 'state/receipts'):
                raise OSError('receipt unavailable')
            return actual(path, value)
        with patch('publisher.put_json', side_effect=interrupt):
            with self.assertRaises(OSError):
                publish(self.entry(), self.reg)
        with lock(self.base):
            sync_index(self.reg)
        self.assertEqual(recall(self.reg, 'alpha', '本地')['cards'], [])
        receipt = publish(self.entry(), self.reg)
        self.assertIn(receipt['status'], ('published', 'unchanged'))
        self.assertTrue(recall(self.reg, 'alpha', '本地')['cards'])

    def test_failed_update_does_not_replace_recalled_lesson(self):
        e = self.entry()
        e['summary'] = 'acceptedoriginal'
        publish(e, self.reg)
        e['summary'] = 'uncommittedrevision'
        with patch('publisher.rebuild', side_effect=RuntimeError('interruption')):
            with self.assertRaises(RuntimeError):
                publish(e, self.reg)
        with lock(self.base):
            sync_index(self.reg)
        self.assertEqual(recall(self.reg, 'alpha', 'uncommittedrevision')['cards'], [])
        self.assertTrue(recall(self.reg, 'alpha', 'acceptedoriginal')['cards'])

    def test_review_only_update_preserves_previous_accepted_record(self):
        e = self.entry()
        e['summary'] = 'acceptedoriginal'
        publish(e, self.reg)
        e['status'] = 'verified'
        e['verification'] = {'result': 'passed', 'evidence_ids': [], 'summary': 'No actual test'}
        self.assertEqual(publish(e, self.reg)['status'], 'needs_review')
        with lock(self.base):
            sync_index(self.reg)
        self.assertTrue(recall(self.reg, 'alpha', 'acceptedoriginal')['cards'])

    def test_corrupt_cache_does_not_cancel_completed_publication(self):
        p = self.base / 'state/knowledge.sqlite3'
        p.parent.mkdir(exist_ok=True)
        p.write_bytes(b'broken sqlite database')
        receipt = publish(self.entry(), self.reg)
        self.assertEqual(receipt['status'], 'published')
        self.assertEqual(receipt['index']['status'], 'stale')
        self.assertEqual(read_json(self.base / 'state/receipts/alpha/ADR-001.json')['content_hash'], receipt['content_hash'])

    def test_index_command_repairs_corrupt_database(self):
        publish(self.entry(), self.reg)
        p = self.base / 'state/knowledge.sqlite3'
        damaged = b'broken sqlite database'
        p.write_bytes(damaged)
        process = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'journal.py'),
            '--config', str(self.config), 'index'], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertTrue(recall(self.reg, 'alpha', '本地')['cards'])
        self.assertTrue(any(x.read_bytes() == damaged for x in (self.base / 'state/index-history').glob('*')))

    def test_cached_orphan_is_not_recalled(self):
        publish(self.entry(), self.reg)
        (self.base / 'state/receipts/alpha/ADR-001.json').unlink()
        self.assertEqual(recall(self.reg, 'alpha', '本地')['cards'], [])

    def test_legacy_interrupted_revision_is_not_pinned_as_accepted(self):
        e = self.entry()
        receipt = publish(e, self.reg)
        saved = read_json(self.base / 'state/entries/alpha/ADR-001.json')
        receipt.pop('record_hash')
        put_json(self.base / 'state/receipts/alpha/ADR-001.json', receipt)
        # Exact state reachable with the old publisher: new note + record,
        # old successful receipt, then rebuild interruption.
        saved['summary'] = 'legacyuncommittedrevision'
        put_json(self.base / 'state/entries/alpha/ADR-001.json', saved)
        Path(receipt['path']).write_text(render_entry(self.reg, saved), encoding='utf-8')
        with lock(self.base):
            sync_index(self.reg)
        self.assertEqual(recall(self.reg, 'alpha', 'legacyuncommittedrevision')['cards'], [])

    def test_unrelated_project_preserves_pending_shared_note(self):
        import publisher
        e = self.entry()
        rel = lesson_path(e['lessons'][0])
        actual = publisher.put_json
        def interrupt(path, value):
            if Path(path) == self.base / 'state/owned.json' and rel in value and 'pending' not in value[rel]:
                raise RuntimeError('shared note ownership interruption')
            return actual(path, value)
        with patch('publisher.put_json', side_effect=interrupt):
            with self.assertRaises(RuntimeError):
                publish(e, self.reg)
        before = (self.reg.vault / rel).read_bytes()
        beta = self.entry(pid='beta')
        beta['lessons'] = []
        self.assertEqual(publish(beta, self.reg)['status'], 'published')
        self.assertEqual((self.reg.vault / rel).read_bytes(), before)
        self.assertEqual(publish(e, self.reg)['status'], 'published')

    def test_legacy_receipt_accepts_original_even_if_user_edits_note(self):
        receipt = publish(self.entry(), self.reg)
        receipt.pop('record_hash')
        put_json(self.base / 'state/receipts/alpha/ADR-001.json', receipt)
        Path(receipt['path']).write_text('User edited note', encoding='utf-8')
        with lock(self.base):
            sync_index(self.reg)
        self.assertTrue(recall(self.reg, 'alpha', '本地')['cards'])

    def test_timeout_ends_descendant_holding_stdout(self):
        marker = self.base / 'alpha/survived.txt'
        child = 'import time,pathlib; time.sleep(3); pathlib.Path(' + repr(str(marker)) + ').write_text("alive")'
        parent = 'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); print("spawned",flush=True); time.sleep(10)'
        timer = threading.Timer
        started = time.monotonic()
        with patch('threading.Timer', side_effect=lambda _, callback: timer(0.6, callback)):
            result = run_test(self.reg, 'alpha', 'timeout', 'tree', [sys.executable, '-c', parent])
        elapsed = time.monotonic() - started
        event = read_json(result['path'])
        self.assertTrue(event['timed_out'])
        self.assertNotEqual(event['exit_code'], 0)
        self.assertLess(elapsed, 2.8)
        time.sleep(max(0, 3.5 - elapsed))
        self.assertFalse(marker.exists(), 'Descendant survived the deadline')


if __name__ == '__main__':
    unittest.main()
