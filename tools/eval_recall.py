"""Measure recall ranking on a labelled query set without touching the live registry.

Copies the accepted records and receipts of a registry into a temporary base,
builds a fresh index there, and reports Hit@k and MRR for keyword, vector and
hybrid ranking. Example:

    python tools/eval_recall.py --config D:/path/projects.json \
        --dataset evaluation/local/recall-queries.json --model BAAI/bge-small-zh-v1.5
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from journal_core import Registry, lock, put_json, read_json  # noqa: E402
import knowledge  # noqa: E402
import semantic  # noqa: E402

COPIED = ('state/entries', 'state/receipts', 'state/project-names')


def snapshot(config, tmp, model):
    """Accepted records and receipts are enough to rebuild the index; the Vault is only read."""
    source = Path(config).resolve().parent
    for rel in COPIED:
        if (source / rel).is_dir():
            shutil.copytree(source / rel, tmp / rel)
    data = read_json(config)
    data.pop('semantic_model', None)
    if model:
        data['semantic_model'] = model
    put_json(tmp / 'projects.json', data)
    reg = Registry(tmp / 'projects.json')
    started = time.perf_counter()
    with lock(reg.base):
        built = knowledge.sync_index(reg)
    built['seconds'] = round(time.perf_counter() - started, 2)
    return reg, built


def vector_only(reg, pid, query):
    """Rank by embeddings alone, keeping recall's own-project-first scope rule."""
    with knowledge.closing(knowledge.connect(reg)) as db:
        order = semantic.search(db, semantic.model_name(reg), query, pid)
        scope = {r: (p, proj) for r, p, proj in db.execute(
            "SELECT rowid,payload,project FROM cards WHERE project=? OR shared='1'", (pid,))}
    order.sort(key=lambda r: scope[r][1] != pid)
    return [json.loads(scope[r][0]) for r in order]


def records(cards):
    """Distinct records in rank order; several cards of one record count once."""
    seen = []
    for card in cards:
        identity = (card['project_id'], card['entry_id'])
        if identity not in seen:
            seen.append(identity)
    return seen


def first_hit(cards, query):
    """1-based rank of the first relevant record."""
    for position, (pid, eid) in enumerate(records(cards), 1):
        if pid == query['project'] and eid in query['relevant']:
            return position
    return None


def score(ranks):
    n = len(ranks) or 1
    return {'n': len(ranks),
            'hit@1': sum(1 for r in ranks if r and r <= 1) / n,
            'hit@3': sum(1 for r in ranks if r and r <= 3) / n,
            'hit@5': sum(1 for r in ranks if r and r <= 5) / n,
            'mrr@10': sum(1 / r for r in ranks if r and r <= 10) / n}


def evaluate(reg, queries, mode):
    rows, elapsed = [], 0.0
    for q in queries:
        started = time.perf_counter()
        if mode == 'vector':
            cards = vector_only(reg, q['project'], q['query'])
        else:
            cards, info = knowledge.ranked(reg, q['project'], q['query'], use_semantic=mode == 'hybrid')
            if mode == 'hybrid' and info['retrieval'] != 'hybrid':
                raise SystemExit('Semantic ranking unavailable: ' + info.get('semantic_error', 'not configured'))
        elapsed += time.perf_counter() - started
        rows.append({'id': q['id'], 'category': q['category'], 'rank': first_hit(cards, q),
                     'top3': len(records(cards)[:3])})
    labelled = [r for r in rows if r['category'] != 'negative']
    report = {'overall': score([r['rank'] for r in labelled]), 'ms_per_query': round(1000 * elapsed / len(rows), 1)}
    for cat in sorted({r['category'] for r in labelled}):
        report[cat] = score([r['rank'] for r in labelled if r['category'] == cat])
    negatives = [r['top3'] for r in rows if r['category'] == 'negative']
    if negatives:
        # Queries with no relevant record: every card recall would inject is noise.
        report['negative'] = {'n': len(negatives), 'noise_cards_in_top3': sum(negatives) / len(negatives)}
    return report, rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--model', action='append', default=[])
    parser.add_argument('--out')
    args = parser.parse_args()
    queries = read_json(args.dataset)['queries']
    results = {'queries': len(queries), 'runs': {}}
    with tempfile.TemporaryDirectory() as raw:
        reg, built = snapshot(args.config, Path(raw) / 'keyword', None)
        results['index'] = {k: built[k] for k in ('records', 'cards')}
        report, rows = evaluate(reg, queries, 'keyword')
        results['runs']['keyword'] = dict(report, ranks={r['id']: r['rank'] for r in rows})
        for model in args.model:
            started = time.perf_counter()
            semantic.encoder(model)  # Exclude one-off model loading from the per-query latency.
            load = round(time.perf_counter() - started, 2)
            reg, built = snapshot(args.config, Path(raw) / model.replace('/', '_'), model)
            if built.get('semantic', {}).get('status') != 'ready':
                raise SystemExit('Embedding failed: ' + json.dumps(built.get('semantic'), ensure_ascii=False))
            for mode in ('vector', 'hybrid'):
                report, rows = evaluate(reg, queries, mode)
                results['runs'][mode + ':' + model] = dict(report, model_load_s=load, index_s=built['seconds'],
                                                           ranks={r['id']: r['rank'] for r in rows})
    text = json.dumps(results, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
    for name, run in results['runs'].items():
        o = run['overall']
        print(f"{name:55} hit@1 {o['hit@1']:.2f}  hit@3 {o['hit@3']:.2f}  hit@5 {o['hit@5']:.2f}  "
              f"mrr@10 {o['mrr@10']:.3f}  {run['ms_per_query']} ms/query")
        for cat in ('exact', 'paraphrase', 'cross'):
            if cat in run:
                c = run[cat]
                print(f"    {cat:12} n={c['n']:2}  hit@1 {c['hit@1']:.2f}  hit@3 {c['hit@3']:.2f}  mrr@10 {c['mrr@10']:.3f}")
        if 'negative' in run:
            print(f"    negative     n={run['negative']['n']:2}  noise cards in top 3: {run['negative']['noise_cards_in_top3']:.2f}")


if __name__ == '__main__':
    main()
