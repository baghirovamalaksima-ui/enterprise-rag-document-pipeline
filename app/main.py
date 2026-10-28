"""Durable document ingestion and tenant-scoped retrieval reference API."""
import os
import threading
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.responses import JSONResponse

from app.pipeline import worker
from app.provider import select_quotes
from app.retrieval import Encoder
from app.runtime import BoundedRequests, Security, Telemetry
from app.search import Search
from app.store import Conflict, Store


class Document(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')
    title: str = Field(min_length=1, max_length=250, pattern=r'\S')
    content: str = Field(min_length=1, max_length=100_000, pattern=r'\S')


class Batch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    documents: list[Document] = Field(min_length=1, max_length=50)

    @model_validator(mode='after')
    def unique_ids(self):
        if len({d.id for d in self.documents}) != len(self.documents):
            raise ValueError('Duplicate document IDs in batch')
        return self


class Query(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question: str = Field(min_length=1, max_length=1000, pattern=r'\S')
    k: int = Field(default=5, ge=1, le=20)
    mode: Literal['hybrid', 'lexical', 'vector'] = 'hybrid'


class AnswerQuery(Query):
    provider: Literal['extractive', 'openai'] = 'extractive'


def create_app(path=None, keys=None, start_worker=True):
    security = Security(keys)
    telemetry = Telemetry()

    @asynccontextmanager
    async def lifespan(api):
        api.state.store = Store(path or os.getenv('DATABASE_PATH', 'var/documents.db'))
        api.state.search = Search(api.state.store, Encoder(os.getenv('EMBEDDING_MODEL') or None))
        stop = threading.Event()
        thread = threading.Thread(target=worker, args=(api.state.store, stop), daemon=True)
        if start_worker:
            thread.start()
        yield
        stop.set()
        if start_worker:
            thread.join(timeout=30)

    api = FastAPI(title='Enterprise RAG Document Pipeline', version='1.0.0', lifespan=lifespan)
    api.add_middleware(BoundedRequests)

    @api.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({'detail': 'Invalid request schema'}, status_code=422)

    @api.get('/healthz')
    def health():
        return {'status': 'ok'}

    @api.get('/readyz')
    def ready():
        with api.state.store.connection() as db:
            db.execute('SELECT 1').fetchone()
        return {'ready': True}

    @api.get('/metrics')
    def metrics(tenant=Depends(security.tenant)):
        return telemetry.snapshot()

    @api.post('/v1/ingest', status_code=202)
    def ingest(batch: Batch, tenant=Depends(security.tenant),
               idempotency_key: str = Header(min_length=8, max_length=100)):
        try:
            job_id = api.state.store.submit(tenant, idempotency_key,
                                            [d.model_dump() for d in batch.documents])
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        telemetry.increment('ingestion_requests')
        return {'job_id': job_id, 'status_url': f'/v1/jobs/{job_id}'}

    @api.get('/v1/jobs/{job_id}')
    def job(job_id: str, tenant=Depends(security.tenant)):
        result = api.state.store.job(tenant, job_id)
        if result is None:
            raise HTTPException(404, 'Job not found')
        return result

    @api.get('/v1/documents/{doc_id}')
    def document(doc_id: str, tenant=Depends(security.tenant)):
        result = api.state.store.document(tenant, doc_id)
        if result is None:
            raise HTTPException(404, 'Document not found')
        return result

    @api.delete('/v1/documents/{doc_id}', status_code=204)
    def delete(doc_id: str, tenant=Depends(security.tenant)):
        if not api.state.store.delete(tenant, doc_id):
            raise HTTPException(404, 'Document not found')
        return Response(status_code=204)

    @api.post('/v1/search')
    def search(query: Query, tenant=Depends(security.tenant)):
        telemetry.increment('search_requests')
        return {'results': api.state.search.query(tenant, query.question, query.k, query.mode)}

    @api.post('/v1/answer')
    def answer(query: AnswerQuery, tenant=Depends(security.tenant)):
        evidence = api.state.search.query(tenant, query.question, query.k, query.mode)
        claims = (select_quotes(query.question, evidence) if query.provider == 'openai' else
                  [{'source': r['id'], 'quote': r['text']} for r in evidence[:1]])
        telemetry.increment('answered' if claims else 'abstained')
        return {'status': 'answered' if claims else 'abstained', 'claims': claims,
                'verification': 'exact quotation attribution only; relevance is not guaranteed'}

    return api
