"""Transactional SQLite document store and leased ingestion queue."""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    tenant TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL,
                    content TEXT NOT NULL, digest TEXT NOT NULL, fields TEXT NOT NULL,
                    PRIMARY KEY (tenant,id));
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, doc_id TEXT NOT NULL,
                    text TEXT NOT NULL, ordinal INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS chunks_tenant ON chunks(tenant,doc_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
                    text, chunk_id UNINDEXED, tenant UNINDEXED, tokenize='unicode61');
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, idem TEXT NOT NULL,
                    digest TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, lease REAL NOT NULL DEFAULT 0,
                    token TEXT, error TEXT, created REAL NOT NULL,
                    UNIQUE(tenant,idem));
                CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status,created);
                CREATE TABLE IF NOT EXISTS revision (tenant TEXT PRIMARY KEY, value INTEGER NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=15000')
        try:
            yield db
        finally:
            db.close()

    def submit(self, tenant, idem, documents):
        payload = json.dumps(documents, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE tenant=? AND idem=?', (tenant, idem)).fetchone()
            if row:
                if row['digest'] != digest:
                    db.rollback()
                    raise Conflict('Idempotency key already used with a different payload')
                db.commit()
                return row['id']
            job = str(uuid.uuid4())
            db.execute('INSERT INTO jobs (id,tenant,idem,digest,payload,status,created) '
                       'VALUES (?,?,?,?,?,?,?)', (job, tenant, idem, digest, payload, 'queued', time.time()))
            db.commit()
            return job

    def claim(self, now=None, lease_seconds=60):
        now = time.time() if now is None else now
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE jobs SET status='failed',error='retry_limit',payload='[]' "
                       "WHERE status='processing' AND lease<? AND attempts>=3", (now,))
            row = db.execute("SELECT * FROM jobs WHERE attempts<3 AND "
                             "(status='queued' OR (status='processing' AND lease<?)) "
                             'ORDER BY created,id LIMIT 1', (now,)).fetchone()
            if row is None:
                db.commit()
                return None
            token = str(uuid.uuid4())
            db.execute("UPDATE jobs SET status='processing',attempts=attempts+1,lease=?,token=? WHERE id=?",
                       (now + lease_seconds, token, row['id']))
            db.commit()
            return {**dict(row), 'token': token}

    def complete(self, job, documents):
        """Lease token fences stale workers; all document/FTS/revision writes commit atomically."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute("SELECT token FROM jobs WHERE id=? AND status='processing'",
                                 (job['id'],)).fetchone()
            if current is None or current['token'] != job['token']:
                db.rollback()
                return False
            for document, chunks, fields in documents:
                tenant, doc_id = job['tenant'], document['id']
                old = db.execute('SELECT digest FROM documents WHERE tenant=? AND id=?',
                                 (tenant, doc_id)).fetchone()
                digest = hashlib.sha256((document['title'] + '\n' + document['content']).encode()).hexdigest()
                if old and old['digest'] == digest:
                    continue
                self._remove(db, tenant, doc_id)
                db.execute('INSERT INTO documents VALUES (?,?,?,?,?,?)',
                           (tenant, doc_id, document['title'], document['content'], digest, json.dumps(fields)))
                for ordinal, text in enumerate(chunks):
                    chunk_id = hashlib.sha256(f'{tenant}\0{doc_id}\0{digest}\0{ordinal}'.encode()).hexdigest()
                    db.execute('INSERT INTO chunks VALUES (?,?,?,?,?)',
                               (chunk_id, tenant, doc_id, text, ordinal))
                    db.execute('INSERT INTO search (text,chunk_id,tenant) VALUES (?,?,?)',
                               (text, chunk_id, tenant))
            db.execute('INSERT INTO revision VALUES (?,1) ON CONFLICT(tenant) '
                       'DO UPDATE SET value=value+1', (job['tenant'],))
            db.execute("UPDATE jobs SET status='completed',payload='[]',error=NULL WHERE id=?", (job['id'],))
            db.commit()
            return True

    @staticmethod
    def _remove(db, tenant, doc_id):
        db.execute('DELETE FROM search WHERE tenant=? AND chunk_id IN '
                   '(SELECT id FROM chunks WHERE tenant=? AND doc_id=?)', (tenant, tenant, doc_id))
        db.execute('DELETE FROM chunks WHERE tenant=? AND doc_id=?', (tenant, doc_id))
        db.execute('DELETE FROM documents WHERE tenant=? AND id=?', (tenant, doc_id))

    def fail(self, job):
        with self.connection() as db:
            db.execute("UPDATE jobs SET status=CASE WHEN attempts>=3 THEN 'failed' ELSE 'queued' END, "
                       "error='processing_failed',payload=CASE WHEN attempts>=3 THEN '[]' ELSE payload END "
                       'WHERE id=? AND token=?', (job['id'], job['token']))

    def delete(self, tenant, doc_id):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            found = db.execute('SELECT 1 FROM documents WHERE tenant=? AND id=?', (tenant, doc_id)).fetchone()
            self._remove(db, tenant, doc_id)
            db.execute('INSERT INTO revision VALUES (?,1) ON CONFLICT(tenant) '
                       'DO UPDATE SET value=value+1', (tenant,))
            db.commit()
            return bool(found)

    def job(self, tenant, job_id):
        with self.connection() as db:
            row = db.execute('SELECT id,status,attempts,error FROM jobs WHERE tenant=? AND id=?',
                             (tenant, job_id)).fetchone()
            return dict(row) if row else None

    def document(self, tenant, doc_id):
        with self.connection() as db:
            row = db.execute('SELECT id,title,fields FROM documents WHERE tenant=? AND id=?',
                             (tenant, doc_id)).fetchone()
            if not row:
                return None
            return {**dict(row), 'fields': json.loads(row['fields'])}

    def snapshot(self, tenant):
        with self.connection() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT value FROM revision WHERE tenant=?', (tenant,)).fetchone()
            records = [dict(r) for r in db.execute('SELECT id,doc_id,text FROM chunks WHERE tenant=? '
                                                 'ORDER BY id', (tenant,))]
            db.commit()
            return row['value'] if row else 0, records

    def revision(self, tenant):
        with self.connection() as db:
            row = db.execute('SELECT value FROM revision WHERE tenant=?', (tenant,)).fetchone()
            return row['value'] if row else 0

    def lexical(self, tenant, terms, limit=50):
        if not terms:
            return []
        query = ' OR '.join('"' + term.replace('"', '""') + '"' for term in sorted(set(terms))[:40])
        with self.connection() as db:
            return [r['chunk_id'] for r in db.execute('SELECT chunk_id FROM search '
                    'WHERE search MATCH ? AND tenant=? ORDER BY bm25(search),chunk_id LIMIT ?',
                    (query, tenant, limit))]
