import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_journal
from hook import handle
from journal_core import Registry, JournalError, lock, read_json, put_json
from knowledge import recall, sync_index, index_summary
from publisher import publish
from journal import graph_audit


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_journal.JournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.reg, self.base = self.fixture.reg, self.fixture.base

    def entry(self, eid='PIT-SEARCH', pid='alpha', shared=True):
        e = self.fixture.entry(eid,pid,typ='pitfall')
        e.update(symptom='Windows 沙箱启动失败，文件占用错误', root_cause_status='unknown',
                 title='Windows 沙箱文件占用', summary='先检查文件占用和工作目录权限，原因待确认。')
        e['lessons'] = [{'text':'Windows sandbox 文件占用时先核对执行环境。',
                         'conditions':'适用于本地 Windows 沙箱启动错误。','reusable':shared}]
        return e

    def explained(self):
        e = self.fixture.entry('ADR-EXPLAIN')
        e['explanation'] = {'project_goal':'将工程经验保存为可核验的本地笔记。',
                            'change_role':'发布器负责把结构化草稿变成 Markdown。',
                            'flow':['从本地草稿读入 JSON。','核验证据与项目身份。','原子写入笔记并返回回执。'],
                            'terms':[{'term':'receipt / 回执','meaning':'说明实际写入结果的路径和哈希。'}],
                            'example':'示例：同一问题再次修复时更新原有记录。',
                            'evidence_ids':['REQ']}
        return e

    def set_mode(self,mode):
        data = read_json(self.reg.path)
        data['projects']['alpha']['note_mode'] = mode
        put_json(self.reg.path,data)
        self.reg = Registry(self.reg.path)

    def test_chinese_and_identifier_search(self):
        publish(self.entry(),self.reg)
        for query in ['沙箱文件占用','Windows sandbox']:
            cards = recall(self.reg,'alpha',query)['cards']
            self.assertEqual(cards[0]['entry_id'],'PIT-SEARCH')
            self.assertEqual(cards[0]['freshness'],'source_hashes_current')

    def test_other_project_only_exposes_explicit_shared_lesson(self):
        foreign = self.entry('PIT-PRIVATE','beta',False)
        foreign['summary'] = 'Windows PRIVATE_INCIDENT_CONTENT'
        publish(foreign,self.reg)
        shared = self.entry('PIT-SHARED','beta',True)
        shared['summary'] = 'PRIVATE_INCIDENT_CONTENT'
        publish(shared,self.reg)
        result = recall(self.reg,'alpha','Windows 沙箱')
        self.assertEqual([c['entry_id'] for c in result['cards']],['PIT-SHARED'])
        self.assertEqual(result['cards'][0]['kind'],'shared_lesson')
        self.assertNotIn('PRIVATE_INCIDENT_CONTENT',json.dumps(result))

    def test_output_is_bounded_and_notes_are_not_scanned(self):
        for i in range(8):
            e = self.entry('PIT-LONG-' + str(i))
            e['summary'] = 'Windows ' + '很长的总结' * 500
            publish(e,self.reg)
        (self.reg.vault / 'Unrelated.md').write_text('UNRELATED_SECRET',encoding='utf-8')
        result = recall(self.reg,'alpha','Windows',max_chars=1800)
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False)),1800)
        self.assertLessEqual(len(result['cards']),3)
        self.assertNotIn('UNRELATED_SECRET',json.dumps(result))

    def test_changed_source_and_stale_index_are_flagged(self):
        publish(self.entry(),self.reg)
        self.fixture.source.write_text('Changed current source',encoding='utf-8')
        self.assertEqual(recall(self.reg,'alpha','沙箱')['cards'][0]['freshness'],'source_changed')
        path = self.base / 'state/entries/alpha/PIT-SEARCH.json'
        record = read_json(path)
        record['summary'] = 'Out of band change'
        put_json(path,record)
        # An out-of-band record revision has no matching publication receipt.
        self.assertEqual(recall(self.reg,'alpha','沙箱')['cards'], [])

    def test_index_incremental_updates_and_removes_deleted_records(self):
        e = self.entry()
        e['summary'] = 'UniqueOldTerm'
        publish(e,self.reg)
        self.assertTrue(recall(self.reg,'alpha','UniqueOldTerm')['cards'])
        e['summary'] = 'UniqueNewTerm'
        publish(e,self.reg)
        self.assertFalse(recall(self.reg,'alpha','UniqueOldTerm')['cards'])
        with lock(self.reg.base):
            self.assertEqual(sync_index(self.reg)['changed_records'],0)
        (self.base / 'state/entries/alpha/PIT-SEARCH.json').unlink()
        with lock(self.reg.base):
            sync_index(self.reg)
        self.assertFalse(recall(self.reg,'alpha','UniqueNewTerm')['cards'])

    def test_session_start_sends_metadata_not_arbitrary_lessons(self):
        publish(self.entry(),self.reg)
        with patch('hook.read_json',side_effect=AssertionError('Should not read all entries')):
            result = handle('start',{'cwd':str(self.base / 'alpha'),'session_id':'s'},self.reg)
        context = json.loads(result['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('reusable_lessons',context)
        self.assertEqual(context['knowledge_index']['project_records'],1)
        self.assertIn('recall',context['workflow'])

    def test_unknown_project_invalid_limits_and_literal_query(self):
        publish(self.entry(),self.reg)
        with self.assertRaises(JournalError):
            recall(self.reg,'unregistered','Windows')
        with self.assertRaises(JournalError):
            recall(self.reg,'alpha','Windows',limit=100)
        result = recall(self.reg,'alpha','" OR * ; DROP TABLE cards; -- Windows')
        self.assertIsInstance(result['cards'],list)
        self.assertEqual(index_summary(self.reg,'alpha')['project_records'],1)

    def test_mode_cli_is_project_scoped_and_reversible(self):
        script = Path(__file__).resolve().parents[1] / 'journal.py'
        def run(mode):
            p = subprocess.run([sys.executable,str(script),'--config',str(self.reg.path),
                                'mode','--project','alpha','--set',mode],capture_output=True)
            self.assertEqual(p.returncode,0,p.stderr)
        run('explanatory')
        reg = Registry(self.reg.path)
        self.assertEqual(reg.project('alpha')['note_mode'],'explanatory')
        self.assertEqual(reg.project('beta').get('note_mode','concise'),'concise')
        run('concise')
        self.assertEqual(Registry(self.reg.path).project('alpha')['note_mode'],'concise')

    def test_explanatory_mode_requires_fresh_draft_explanation(self):
        old = publish(self.fixture.entry(),self.reg)
        before = Path(old['path']).read_bytes()
        self.set_mode('explanatory')
        receipt = publish(self.fixture.entry(),self.reg)
        self.assertEqual(receipt['status'],'needs_review')
        self.assertEqual(Path(old['path']).read_bytes(),before)

    def test_explanation_needs_source_and_valid_reference(self):
        e = self.explained()
        e['explanation']['evidence_ids'] = ['MISSING']
        self.assertEqual(publish(e,self.reg)['status'],'needs_review')

    def test_explanation_creates_linked_guide_and_preserves_history(self):
        self.set_mode('explanatory')
        e = self.explained()
        first = publish(e,self.reg)
        self.assertEqual(first['status'],'published')
        guide = self.reg.vault / 'Engineering/Projects/alpha/Project-Guide 项目讲解.md'
        self.assertIn('原子写入',guide.read_text(encoding='utf-8'))
        self.assertFalse(graph_audit(self.reg.vault)['unresolved'])
        self.set_mode('concise')
        del e['explanation']
        e['summary'] = 'Updated engineering result'
        self.assertEqual(publish(e,self.reg)['status'],'published')
        saved = read_json(self.base / 'state/entries/alpha/ADR-EXPLAIN.json')
        self.assertIn('explanation',saved)
        self.assertIn('## 项目讲解',Path(first['path']).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
