"""Tenant-isolated FAISS + SQLite FTS5 reciprocal-rank fusion."""
import threading
from collections import OrderedDict

import faiss

from app.retrieval import tokens


class Search:
    def __init__(self, store, encoder, max_cached_tenants=4):
        self.store, self.encoder = store, encoder
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.max_cached_tenants = max_cached_tenants

    def _snapshot(self, tenant):
        with self.lock:
            revision = self.store.revision(tenant)
            if tenant not in self.cache or self.cache[tenant][0] != revision:
                revision, records = self.store.snapshot(tenant)
                index = faiss.IndexFlatIP(self.encoder.dim)
                # Batches bound intermediate embedding memory; flat index itself remains in RAM.
                for start in range(0, len(records), 128):
                    index.add(self.encoder.encode([r['text'] for r in records[start:start + 128]]))
                self.cache[tenant] = (revision, records, index)
            self.cache.move_to_end(tenant)
            while len(self.cache) > self.max_cached_tenants:
                self.cache.popitem(last=False)
            return self.cache[tenant]

    def query(self, tenant, question, k=5, mode='hybrid'):
        terms = tokens(question)
        if not terms:
            return []
        revision, records, index = self._snapshot(tenant)
        if not records:
            return []
        by_id = {r['id']: r for r in records}
        rankings = []
        if mode in ('hybrid', 'lexical'):
            rankings.append(self.store.lexical(tenant, terms))
        if mode in ('hybrid', 'vector'):
            distances, ids = index.search(self.encoder.encode([question]), min(50, len(records)))
            candidates = [(float(s), records[int(i)]['id']) for s, i in zip(distances[0], ids[0])
                          if i >= 0 and s >= 0.10]
            candidates.sort(key=lambda pair: (-pair[0], pair[1]))
            rankings.append([cid for _, cid in candidates])
        scores = {}
        for ranking in rankings:
            for rank, cid in enumerate(ranking, 1):
                if cid in by_id:
                    scores[cid] = scores.get(cid, 0) + 1 / (60 + rank)
        # A concurrent commit/delete invalidates the result; caller retries against fresh state.
        if self.store.revision(tenant) != revision:
            return []
        return [{**by_id[cid], 'score': score} for cid, score in
                sorted(scores.items(), key=lambda row: (-row[1], row[0]))[:k]]
