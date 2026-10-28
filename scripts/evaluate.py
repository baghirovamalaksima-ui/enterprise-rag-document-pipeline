"""Synthetic retrieval/load experiment with exact topic tokens; no generalization claim."""
import argparse
import hashlib
import json
import platform
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.pipeline import run_once  # noqa: E402
from app.retrieval import Encoder  # noqa: E402
from app.search import Search  # noqa: E402
from app.store import Store  # noqa: E402


def evaluate(count=1000):
    if count < 50:
        raise ValueError('At least 50 documents required')
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / 'evaluation.db')
        encoder = Encoder()
        started = time.perf_counter()
        digest = hashlib.sha256()
        for start in range(0, count, 50):
            documents = [{'id': f'doc-{i}', 'title': f'Policy {i}', 'content':
                          f'Topic{i:06d} policy requires approval from department{i:06d}. '
                          f'Reference DOC-{i} is effective 2026-10-28 with budget USD {100 + i}.00.'}
                         for i in range(start, min(start + 50, count))]
            digest.update(json.dumps(documents, sort_keys=True).encode())
            store.submit('benchmark', f'batch-{start}', documents)
            run_once(store)
        ingestion_seconds = time.perf_counter() - started
        search = Search(store, encoder)
        started = time.perf_counter()
        search._snapshot('benchmark')
        index_seconds = time.perf_counter() - started
        for mode in ['lexical', 'vector', 'hybrid']:
            for i in range(0, count, max(1, count // 50)):
                started = time.perf_counter()
                hits = search.query('benchmark', f'Topic{i:06d} policy approval', 5, mode)
                doc_ids = [r['doc_id'] for r in hits]
                rank = doc_ids.index(f'doc-{i}') + 1 if f'doc-{i}' in doc_ids else None
                rows.append({'mode': mode, 'query_id': f'q-{i}', 'target': f'doc-{i}',
                             'rank': rank, 'retrieved': doc_ids,
                             'latency_ms': (time.perf_counter() - started) * 1000})
        summaries = {}
        for mode in ['lexical', 'vector', 'hybrid']:
            selected = [r for r in rows if r['mode'] == mode]
            latencies = sorted(r['latency_ms'] for r in selected)
            summaries[mode] = {'queries': len(selected),
                               'recall@5': sum(r['rank'] is not None for r in selected) / len(selected),
                               'mrr@5': sum(1 / r['rank'] if r['rank'] else 0 for r in selected) / len(selected),
                               'p95_warm_latency_ms': latencies[int(.95 * (len(latencies) - 1))]}
        assert search.query('other-tenant', 'policy approval') == []
        document_count = len(store.snapshot('benchmark')[1])
    for path in [*sorted((ROOT / 'app').glob('*.py')), Path(__file__)]:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return {'metadata': {'python': platform.python_version(), 'platform': platform.platform(),
                         'encoder': encoder.model_name, 'input_and_code_sha256': digest.hexdigest(),
                         'dataset': 'Generated synthetic one-chunk policies with unique exact-match topic tokens. '
                         'Development/load smoke test, not a held-out semantic benchmark.'},
            'summary': {'documents': document_count, 'ingestion_seconds': ingestion_seconds,
                        'documents_per_second': count / ingestion_seconds,
                        'cold_index_build_seconds': index_seconds, 'retrieval': summaries},
            'rows': rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--documents', type=int, default=1000)
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/evaluation.json')
    args = parser.parse_args()
    report = evaluate(args.documents)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report['summary'], indent=2))
