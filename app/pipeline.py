"""Bounded text chunking, typed extraction, and restartable worker."""
import json
import re
import time


def chunk_text(text, size=120, overlap=20):
    if size <= overlap or overlap < 0:
        raise ValueError('Require 0 <= overlap < size')
    words = text.split()
    chunks = []
    for start in range(0, len(words), size - overlap):
        chunks.append(' '.join(words[start:start + size]))
        if start + size >= len(words):
            break
    return chunks


def extract_fields(text):
    """Transparent rule-based extraction; no claim of a trained NER model."""
    return {
        'dates': sorted(set(re.findall(r'\b\d{4}-\d{2}-\d{2}\b', text))),
        'amounts': sorted(set(re.findall(r'\b(?:USD|EUR|AZN)\s+\d+(?:\.\d{2})?\b', text))),
        'references': sorted(set(re.findall(r'\b(?:INV|DOC|PO)-\d+\b', text))),
    }


def run_once(store):
    job = store.claim()
    if job is None:
        return False
    try:
        docs = json.loads(job['payload'])
        prepared = [(d, chunk_text(d['content']), extract_fields(d['content'])) for d in docs]
        store.complete(job, prepared)
    except Exception:
        # Store error code only: exception strings can contain document content.
        store.fail(job)
    return True


def worker(store, stop):
    while not stop.is_set():
        if not run_once(store):
            stop.wait(0.25)
        else:
            time.sleep(0.01)
