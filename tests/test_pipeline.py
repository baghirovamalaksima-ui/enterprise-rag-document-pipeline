import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.pipeline import chunk_text, extract_fields, run_once
from app.retrieval import Encoder, tokens
from app.search import Search
from app.store import Conflict, Store

KEY_A = 'test-enterprise-key-tenant-a-12345'
KEY_B = 'test-enterprise-key-tenant-b-12345'
DOC = {'id': 'invoice', 'title': 'Invoice',
       'content': 'Invoice INV-2026 payment of USD 250.00 is due on 2026-11-15. Payment requires approval.'}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / 'documents.db')


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / 'api.db', {KEY_A: 'a', KEY_B: 'b'}, start_worker=False)) as c:
        yield c


def populated(store, tenant='a'):
    job = store.submit(tenant, 'first-batch', [DOC])
    assert run_once(store)
    assert store.job(tenant, job)['status'] == 'completed'
    return job


def test_chunking_and_extraction():
    text = ' '.join(str(i) for i in range(251))
    chunks = chunk_text(text)
    assert len(chunks) == 3
    assert chunks[0].split()[-20:] == chunks[1].split()[:20]
    assert set(' '.join(chunks).split()) == set(text.split())
    assert chunk_text('') == []
    with pytest.raises(ValueError):
        chunk_text('x', 10, 10)
    assert extract_fields(DOC['content']) == {'dates': ['2026-11-15'], 'amounts': ['USD 250.00'],
                                              'references': ['INV-2026']}


def test_idempotency_and_conflict(store):
    one = store.submit('a', 'same-key', [DOC])
    assert store.submit('a', 'same-key', [DOC]) == one
    with pytest.raises(Conflict):
        store.submit('a', 'same-key', [{**DOC, 'content': 'changed'}])
    assert store.submit('b', 'same-key', [DOC]) != one


def test_atomic_claim_and_fencing(store):
    store.submit('a', 'lease-test', [DOC])
    old = store.claim(now=1, lease_seconds=1)
    fresh = store.claim(now=3, lease_seconds=60)
    prepared = [(DOC, chunk_text(DOC['content']), {})]
    assert not store.complete(old, prepared)
    assert store.document('a', 'invoice') is None
    assert store.complete(fresh, prepared)


def test_concurrent_claim_only_once(store):
    store.submit('a', 'concurrent', [DOC])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.claim(), range(4)))
    assert sum(r is not None for r in results) == 1


def test_retry_and_dead_letter(store):
    job_id = store.submit('a', 'retry-job', [DOC])
    for _ in range(3):
        job = store.claim()
        assert job is not None
        store.fail(job)
    assert store.job('a', job_id)['status'] == 'failed'
    assert store.claim() is None


def test_crashed_worker_retry_limit(store):
    job_id = store.submit('a', 'crash-job', [DOC])
    for now in [1, 3, 5]:
        assert store.claim(now=now, lease_seconds=1)
    assert store.claim(now=7) is None
    assert store.job('a', job_id)['status'] == 'failed'


@pytest.mark.parametrize('mode', ['hybrid', 'lexical', 'vector'])
def test_search_tenant_isolation_and_persistence(store, mode):
    populated(store)
    reopened = Store(store.path)
    search = Search(reopened, Encoder())
    results = search.query('a', 'invoice payment', mode=mode)
    assert results and results[0]['doc_id'] == 'invoice'
    assert search.query('b', 'invoice payment', mode=mode) == []
    assert search.query('a', 'zzzzunmatchedterm', mode='lexical') == []
    assert search.query('a', '   ') == []


def test_replacement_delete_and_cache_invalidation(store):
    populated(store)
    search = Search(store, Encoder())
    assert search.query('a', 'invoice payment')
    updated = {**DOC, 'content': 'Banana orchard harvesting season begins tomorrow.'}
    store.submit('a', 'replacement', [updated])
    run_once(store)
    assert not search.query('a', 'invoice payment', mode='lexical')
    assert search.query('a', 'banana orchard')
    assert store.delete('a', DOC['id'])
    assert not search.query('a', 'banana orchard')
    assert not store.delete('b', DOC['id'])


def test_duplicate_ingestion_does_not_duplicate_chunks(store):
    populated(store)
    store.submit('a', 'second-job', [DOC])
    run_once(store)
    assert len(store.snapshot('a')[1]) == 1


def test_atomic_batch_rollback(store):
    store.submit('a', 'rollback', [DOC])
    job = store.claim()
    with pytest.raises(KeyError):
        store.complete(job, [(DOC, ['valid chunk'], {}), ({'id': 'broken'}, [], {})])
    assert store.document('a', DOC['id']) is None
    assert store.snapshot('a')[1] == []


def test_worker_failure_does_not_expose_payload(store, monkeypatch):
    job_id = store.submit('a', 'failure', [DOC])
    def fail(*args):
        raise RuntimeError('Sensitive document must not appear in error field')
    monkeypatch.setattr(store, 'complete', fail)
    assert run_once(store)
    job = store.job('a', job_id)
    assert job['error'] == 'processing_failed'
    assert job['status'] == 'queued'


def test_api_ingest_search_delete(client):
    headers = {'X-API-Key': KEY_A, 'Idempotency-Key': 'api-batch-1'}
    response = client.post('/v1/ingest', json={'documents': [DOC]}, headers=headers)
    assert response.status_code == 202
    job = response.json()['job_id']
    assert client.get(f'/v1/jobs/{job}', headers={'X-API-Key': KEY_B}).status_code == 404
    run_once(client.app.state.store)
    assert client.get(f'/v1/jobs/{job}', headers=headers).json()['status'] == 'completed'
    assert client.get('/v1/documents/invoice', headers=headers).json()['fields']['amounts'] == ['USD 250.00']
    answer = client.post('/v1/answer', headers=headers, json={'question': 'invoice payment'})
    assert answer.json()['status'] == 'answered'
    assert client.post('/v1/search', headers={'X-API-Key': KEY_B},
                       json={'question': 'invoice payment'}).json()['results'] == []
    conflict = client.post('/v1/ingest', headers=headers, json={'documents': [{**DOC, 'title': 'different'}]})
    assert conflict.status_code == 409
    assert client.delete('/v1/documents/invoice', headers=headers).status_code == 204
    assert client.get('/v1/documents/invoice', headers=headers).status_code == 404


def test_api_validation_and_auth(client):
    headers = {'X-API-Key': KEY_A, 'Idempotency-Key': 'validation'}
    assert client.post('/v1/search', json={'question': 'payment'}).status_code == 401
    assert client.post('/v1/search', json={'question': 'payment', 'k': 0}, headers=headers).status_code == 422
    assert client.post('/v1/ingest', json={'documents': [DOC, DOC]}, headers=headers).status_code == 422
    assert client.post('/v1/ingest', json={'documents': [{**DOC, 'id': '../escape'}]},
                       headers=headers).status_code == 422
    assert client.post('/v1/ingest', json={'documents': []}, headers=headers).status_code == 422
    assert client.post('/v1/search', content=b'x' * 1_048_577, headers=headers).status_code == 413
    assert client.get('/readyz').json()['ready']


def test_real_background_worker(tmp_path):
    with TestClient(create_app(tmp_path / 'worker.db', {KEY_A: 'a'})) as client:
        headers = {'X-API-Key': KEY_A, 'Idempotency-Key': 'background'}
        job = client.post('/v1/ingest', headers=headers, json={'documents': [DOC]}).json()['job_id']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = client.get(f'/v1/jobs/{job}', headers=headers).json()['status']
            if status == 'completed':
                break
            time.sleep(0.05)
        assert status == 'completed'


def test_unicode_tokens():
    assert 'sənəd' in tokens('Sənəd emalı və ödəniş')


def test_encoder_deterministic():
    import numpy as np
    encoder = Encoder()
    a = encoder.encode(['invoice payment'])
    b = encoder.encode(['invoice payment'])
    np.testing.assert_array_equal(a, b)
    assert np.linalg.norm(a) == pytest.approx(1)


def test_provider_fails_closed(client, monkeypatch):
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    headers = {'X-API-Key': KEY_A, 'Idempotency-Key': 'provider'}
    client.post('/v1/ingest', headers=headers, json={'documents': [DOC]})
    run_once(client.app.state.store)
    assert client.post('/v1/answer', headers=headers,
                       json={'question': 'invoice payment', 'provider': 'openai'}).json()['status'] == 'abstained'
