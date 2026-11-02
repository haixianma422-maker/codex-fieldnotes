from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_journal
import semantic
from journal_core import Registry, lock, read_json, put_json
from knowledge import fuse, recall, sync_index
from publisher import publish

# Concept vectors stand in for a real model: synonyms in different words or languages share one axis.
CONCEPTS = [('闪烁','一闪','flicker'), ('托盘','tray'), ('超时','timeout'), ('图片','image')]


class FakeEncoder:
    def __init__(self):
        self.embedded = 0

    def vector(self, text):
        text = text.lower()
        return [1.0 if any(w in text for w in group) else 0.0 for group in CONCEPTS] + [0.01]

    def embed(self, texts):
        texts = list(texts)
        self.embedded += len(texts)
        return [self.vector(t) for t in texts]

    def query_embed(self, query):
        return [self.vector(query)]


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_journal.JournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.encoder = FakeEncoder()
        semantic._encoders['fake/model'] = self.encoder
        self.addCleanup(semantic._encoders.pop, 'fake/model', None)
        self.reg = self.configure('fake/model')

    def configure(self, model):
        data = read_json(self.fixture.reg.path)
        data['semantic_model'] = model
        put_json(self.fixture.reg.path, data)
        return Registry(self.fixture.reg.path)

    def entry(self, eid, title, summary):
        e = self.fixture.entry(eid, 'alpha', typ='pitfall')
        e.update(title=title, summary=summary, symptom=summary, root_cause_status='unknown')
        e['lessons'] = []
        return e

    def test_fusion_prefers_items_ranked_well_by_both_lists(self):
        self.assertEqual(fuse(['a','b','c'], ['b','c','a'])[0], 'b')
        self.assertEqual(fuse(['x'], []), ['x'])

    def test_hybrid_recall_finds_paraphrase_without_shared_terms(self):
        publish(self.entry('PIT-FLICKER','检测时浮窗一闪一闪','周期识别隐藏窗口导致闪烁。'), self.reg)
        publish(self.entry('PIT-TRAY','托盘启动失败','pystray 权限错误。'), self.reg)
        query = 'overlay flicker during capture'
        result = recall(self.reg, 'alpha', query)
        self.assertEqual(result['retrieval'], 'hybrid')
        self.assertEqual(result['cards'][0]['entry_id'], 'PIT-FLICKER')
        plain = recall(Registry(self.configure_none()), 'alpha', query)
        self.assertEqual(plain['retrieval'], 'keyword')
        self.assertNotIn('PIT-FLICKER', [c['entry_id'] for c in plain['cards']])

    def configure_none(self):
        data = read_json(self.fixture.reg.path)
        data.pop('semantic_model')
        put_json(self.fixture.reg.path, data)
        return self.fixture.reg.path

    def test_vectors_are_incremental_and_follow_deleted_records(self):
        publish(self.entry('PIT-A','图片缺失','离线时 image 不显示。'), self.reg)
        publish(self.entry('PIT-B','超时','timeout 后子进程残留。'), self.reg)
        with lock(self.reg.base):
            again = sync_index(self.reg)
        self.assertEqual(again['semantic']['embedded'], 0)
        (self.fixture.base / 'state/entries/alpha/PIT-A.json').unlink()
        with lock(self.reg.base):
            removed = sync_index(self.reg)
        self.assertEqual(removed['semantic']['removed'], 1)
        self.assertNotIn('PIT-A', [c['entry_id'] for c in recall(self.reg, 'alpha', 'image')['cards']])

    def test_missing_model_keeps_publication_and_keyword_recall(self):
        reg = self.configure('missing/not-a-model')
        receipt = publish(self.entry('PIT-TRAY','托盘启动失败','pystray 权限错误。'), reg)
        self.assertEqual(receipt['status'], 'published')
        self.assertEqual(receipt['index']['status'], 'ready')
        self.assertEqual(receipt['index']['semantic']['status'], 'unavailable')
        result = recall(reg, 'alpha', '托盘')
        self.assertEqual(result['retrieval'], 'keyword')
        self.assertIn('semantic_error', result)
        self.assertEqual(result['cards'][0]['entry_id'], 'PIT-TRAY')


if __name__ == '__main__':
    unittest.main()
