"""FAISS exact cosine search; offline feature hashing or optional local HF model."""
import hashlib
import re
import threading
from collections import Counter

import faiss
import numpy as np

faiss.omp_set_num_threads(1)
STOP = {'a', 'an', 'the', 'is', 'are', 'what', 'how', 'do', 'does', 'i', 'my',
        'to', 'of', 'in', 'and', 'for', 'can', 'please', 'me', 'with'}


def tokens(text):
    return [t for t in re.findall(r'[^\W_]+', text.casefold(), flags=re.UNICODE)
            if t not in STOP and len(t) > 1]


class Encoder:
    def __init__(self, model=None):
        self.model_name = model or 'blake2-word-hash-v1-384'
        self.model = None
        if model:
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(model, local_files_only=True)
        self.dim = self.model.get_sentence_embedding_dimension() if self.model else 384
        self.lock = threading.Lock()

    def encode(self, texts):
        if self.model:
            with self.lock:
                result = self.model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
            return np.ascontiguousarray(result, dtype='float32')
        result = np.zeros((len(texts), self.dim), dtype='float32')
        for row, text in enumerate(texts):
            for token, count in Counter(tokens(text)).items():
                digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
                result[row, int.from_bytes(digest, 'little') % self.dim] += 1 + np.log(count)
        faiss.normalize_L2(result)
        return result


def vector_rank(question, records, encoder, k):
    if not records or not tokens(question):
        return []
    matrix = encoder.encode([r['text'] for r in records])
    index = faiss.IndexFlatIP(encoder.dim)
    index.add(matrix)
    scores, ids = index.search(encoder.encode([question]), len(records))
    pairs = [(float(s), records[int(i)]) for s, i in zip(scores[0], ids[0]) if i >= 0 and s > 0.01]
    pairs.sort(key=lambda row: (-row[0], row[1]['id']))
    return pairs[:k]
