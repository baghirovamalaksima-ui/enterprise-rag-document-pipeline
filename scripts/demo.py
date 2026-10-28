"""Run durable ingestion, extraction, retrieval and tenant isolation through the API."""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

DEMO_KEY = 'in-process-demo-key-not-for-deployment'
OTHER_KEY = 'in-process-other-key-not-for-deployment'
with tempfile.TemporaryDirectory() as tmp:
    with TestClient(create_app(Path(tmp) / 'demo.db', {DEMO_KEY: 'demo', OTHER_KEY: 'other'})) as client:
        headers = {'X-API-Key': DEMO_KEY, 'Idempotency-Key': 'demo-invoice-batch'}
        batch = json.loads((ROOT / 'data/sample-batch.json').read_text(encoding='utf-8'))
        submitted = client.post('/v1/ingest', headers=headers, json=batch)
        submitted.raise_for_status()
        job = submitted.json()['job_id']
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = client.get(f'/v1/jobs/{job}', headers=headers).json()
            if status['status'] in {'completed', 'failed'}:
                break
            time.sleep(0.05)
        if status['status'] != 'completed':
            raise RuntimeError('Demo ingestion did not complete')
        document = client.get('/v1/documents/invoice-2026', headers=headers)
        document.raise_for_status()
        answer = client.post('/v1/answer', headers=headers, json={'question': 'invoice payment approval'})
        answer.raise_for_status()
        isolation = client.post('/v1/search', headers={'X-API-Key': OTHER_KEY},
                                json={'question': 'invoice payment approval'})
        isolation.raise_for_status()
        assert isolation.json()['results'] == []
        print(json.dumps({'job': status, 'document': document.json(), 'answer': answer.json(),
                          'other_tenant_results': isolation.json()['results']}, indent=2))
