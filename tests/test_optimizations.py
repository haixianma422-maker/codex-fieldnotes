import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_journal import JournalTests as Fixtures
import hook
import publisher
from journal_core import digest, lock, put_json, read_json


class OptimizationTests(unittest.TestCase):
    setUp = Fixtures.setUp
    tearDown = Fixtures.tearDown
    entry = Fixtures.entry
    evidence = Fixtures.evidence
    event = Fixtures.event
    stop = Fixtures.stop

    def test_merged_schema_limits_still_apply(self):
        from journal_core import JournalError
        entry = self.entry()
        entry['attempts'] = [{'action': str(i), 'outcome': 'failed', 'evidence_ids': []} for i in range(50)]
        publisher.publish(entry, self.reg)
        entry['attempts'] = [{'action': 'new attempt', 'outcome': 'failed', 'evidence_ids': []}]
        with self.assertRaises(JournalError):
            publisher.publish(entry, self.reg)

    def test_legacy_pending_migration_and_project_isolation(self):
        from event_store import pending_events
        for pid in ('alpha', 'beta'):
            put_json(self.base / f'events/{pid}/{pid}.json',
                     {'project_id': pid, 'event_id': pid, 'meaningful': True})
        with lock(self.base):
            self.assertEqual([e['event_id'] for e in pending_events(self.reg, 'alpha')], ['alpha'])
            self.assertEqual([e['event_id'] for e in pending_events(self.reg, 'beta')], ['beta'])

    def test_corrupt_foreign_history_does_not_block_current_project(self):
        from event_store import pending_events
        foreign = self.base / 'events/beta/broken.json'
        foreign.parent.mkdir(parents=True)
        foreign.write_text('broken history', encoding='utf-8')
        event = self.event(code=0)
        with lock(self.base):
            self.assertEqual([e['event_id'] for e in pending_events(self.reg, 'alpha')], [event['journal_event']])

    def test_invalid_ready_marker_is_rebuilt(self):
        from event_store import pending_events
        first = self.event(code=0)
        with lock(self.base):
            pending_events(self.reg, 'alpha')
        for marker in (self.base / 'state/pending-events').glob('*.json'):
            put_json(marker, [])
        second = self.event(toolid='second', code=0)
        with lock(self.base):
            self.assertEqual({e['event_id'] for e in pending_events(self.reg, 'alpha')},
                             {first['journal_event'], second['journal_event']})

    def test_new_capture_survives_corrupt_own_history(self):
        bad = self.base / 'events/alpha/bad.json'
        bad.parent.mkdir(parents=True)
        bad.write_text('broken old history', encoding='utf-8')
        event = self.event(code=0)
        self.assertEqual(read_json(event['evidence_path'])['exit_code'], 0)

    def test_rebuild_rejects_pointer_junction_without_deleting_external_files(self):
        from event_store import rebuild_pending
        from journal_core import JournalError
        outside = self.base / 'outside'
        outside.mkdir()
        saved = outside / 'keep.json'
        saved.write_text('preserve', encoding='utf-8')
        link = self.base / 'state/pending-events/alpha'
        link.parent.mkdir(parents=True)
        if sys.platform == 'win32':
            process = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr)
        else:
            link.symlink_to(outside, target_is_directory=True)
        try:
            with lock(self.base), self.assertRaises(JournalError):
                rebuild_pending(self.reg)
            self.assertEqual(saved.read_text(encoding='utf-8'), 'preserve')
        finally:
            if sys.platform == 'win32':
                link.rmdir()
            else:
                link.unlink()

    def test_pending_migrates_history_and_normal_reads_skip_acknowledged_history(self):
        event = self.event(code=0)
        eid = event['journal_event']
        with lock(self.base):
            from event_store import pending_events, acknowledge
            self.assertEqual([e['event_id'] for e in pending_events(self.reg, 'alpha')], [eid])
            acknowledge(self.reg, 'alpha', eid, {'reason': 'done'})
        # Once migrated, archived events must not be opened by start or stop.
        (self.base / f'events/alpha/{eid}.json').write_text('invalid archived JSON', encoding='utf-8')
        self.assertEqual(self.stop(), {})
        context = hook.handle('start', {'cwd': str(self.base / 'alpha')}, self.reg)
        self.assertEqual(json.loads(context['hookSpecificOutput']['additionalContext'])['pending_event_ids'], [])

    def test_pending_rebuild_and_ack_after_interrupted_removal(self):
        from event_store import pending_events, acknowledge, rebuild_pending
        event = self.event(code=0)
        eid = event['journal_event']
        with lock(self.base):
            with patch.object(Path, 'unlink', side_effect=OSError('simulated removal interruption')):
                acknowledge(self.reg, 'alpha', eid, {'reason': 'done'})
            self.assertEqual(pending_events(self.reg, 'alpha'), [])
            self.assertEqual(rebuild_pending(self.reg)['pending'], 0)
        self.assertTrue((self.base / f'events/alpha/{eid}.json').exists())

    def test_completed_publication_survives_pending_pointer_cleanup_failure(self):
        event = self.event(code=0)
        entry = self.entry()
        entry['event_ids'] = [event['journal_event']]
        original = Path.unlink
        def fail_cleanup(path, *args, **kwargs):
            if 'pending-events' in path.parts:
                raise OSError('cache cleanup unavailable')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_cleanup):
            self.assertEqual(publisher.publish(entry, self.reg)['status'], 'published')
            self.assertEqual(self.stop(), {})
        self.assertFalse(list((self.base / 'outbox').rglob('*.rejected.json')))

    def test_event_write_interruption_does_not_hide_retry(self):
        import event_store
        event = {'project_id': 'alpha', 'event_id': 'interrupt', 'meaningful': True}
        original = event_store.put_json
        def fail_event(path, value):
            if '/events/' in Path(path).as_posix():
                raise OSError('event write interrupted')
            return original(path, value)
        with lock(self.base):
            with patch.object(event_store, 'put_json', side_effect=fail_event):
                with self.assertRaises(OSError):
                    event_store.write_event(self.reg, event)
            self.assertEqual(event_store.pending_events(self.reg, 'alpha'), [])
            event_store.write_event(self.reg, event)
            self.assertEqual(event_store.pending_events(self.reg, 'alpha'), [event])

    def test_merged_evidence_is_read_once_and_resubmission_rechecks_source(self):
        entry = self.entry()
        publisher.publish(entry, self.reg)
        reads = []
        original = Path.read_bytes
        def track(path):
            if path == self.source:
                reads.append(path)
            return original(path)
        with patch.object(Path, 'read_bytes', track):
            self.assertEqual(publisher.publish(entry, self.reg)['status'], 'unchanged')
        self.assertEqual(len(reads), 1)
        self.source.write_text('source changed', encoding='utf-8')
        self.assertEqual(publisher.publish(entry, self.reg)['status'], 'needs_review')

    def test_finalize_derives_status_without_publishing_or_mutating_evidence(self):
        import finalize
        report = self.base / 'docs/native-codex-runtime.json'
        put_json(report, {'status': 'passed', 'timestamp': '2026-10-10T00:00:00Z'})
        before = report.read_bytes()
        result = finalize.generate_status(self.reg)
        self.assertIn('native-codex-runtime.json', result)
        self.assertIn('passed', result)
        self.assertNotIn('当前状态：脚本与 Obsidian 已验收，原生自动化验收受阻', result)
        self.assertEqual(report.read_bytes(), before)
        self.assertFalse((self.base / 'state/entries').exists())
        self.assertFalse((self.base / 'state/receipts').exists())

    def test_finalize_missing_evidence_does_not_claim_passed(self):
        import finalize
        result = finalize.generate_status(self.reg)
        self.assertIn('未运行', result)
        self.assertNotIn('全部通过', result)

    def test_finalize_reports_malformed_report_as_unconfirmed(self):
        import finalize
        for value in ([], None, {'installed_hashes': []}):
            put_json(self.base / 'docs/native-codex-runtime.json', value)
            self.assertIn('状态未确认', finalize.generate_status(self.reg))

    def test_finalize_keeps_summary_compact_and_links_detailed_report(self):
        import finalize
        report = {'automatic_desktop_hook_e2e': {'status': 'passed_capture_and_publication',
                                                'large_details': 'do-not-inline-full-report' * 1000},
                  'installed_suite': {'tests': 81, 'exit_code': 0}}
        put_json(self.base / 'docs/native-codex-runtime.json', report)
        summary = finalize.generate_status(self.reg)
        self.assertIn('passed_capture_and_publication', summary)
        self.assertIn('tests=81', summary)
        self.assertNotIn('do-not-inline-full-report', summary)

    def test_script_acceptance_does_not_reassert_old_client_or_sandbox_status(self):
        import acceptance
        event = self.event(code=0)
        suite = {'path': event['evidence_path'], 'event_id': event['journal_event']}
        milestone = acceptance.script_milestone(suite)
        self.assertNotIn('阻塞', json.dumps(milestone, ensure_ascii=False))
        self.assertFalse(any('Obsidian' in item or '信任' in item for item in milestone['completed']))
        self.assertEqual(milestone['verification']['result'], 'passed')


del Fixtures
