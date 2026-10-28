# Operations and reproducibility

## Configuration

| Variable | Behavior |
|---|---|
| `TENANT_KEYS` | Required JSON object mapping secret keys (24+ characters) to tenant IDs. Startup rejects empty/weak configuration. |
| `OPENAI_API_KEY` | Optional external provider key. Never committed or included in request logs. |
| `OPENAI_MODEL` | Explicit model identifier with Responses structured-output support; no model is silently substituted. |
| `EMBEDDING_MODEL` | Optional absolute path to an approved local SentenceTransformer model. Default is offline hashing. |
| `DATABASE_PATH` | Enterprise SQLite location; default `var/documents.db`. |

`python scripts/start.py` generates `.env` only if missing, loads it, and starts
the server bound to localhost. `--init-only` prepares credentials for Compose.
On Windows, protect `.env` using filesystem ACLs; POSIX chmod alone is not a
Windows access-control guarantee. `.env` and local databases are excluded from Git.

Use Python 3.12. Runtime packages are pinned to the tested versions. Optional
neural embedding dependencies are intentionally outside the tested runtime lock.
To enable that extension, install `sentence-transformers` in a separate reviewed
environment and supply a local model directory. `local_files_only=True` prevents
runtime downloads, and remote model code is not enabled. Mount model files read-only
and include that environment/model revision in a separate validation report.

## Runtime boundaries

One API process, one local SQLite volume. Rate limits and counters are process-local;
rate limiting is not a distributed service. Readiness checks initialization/storage,
not the availability or correctness of an external model. Requests are capped at
1 MiB actual body bytes, including chunked transfer. Provider requests have a
20-second timeout (5-second connect timeout), no automatic retries, and fail closed.

Do not enable access logs containing payloads or credentials. Telemetry exposes
only aggregate counters behind authentication; any valid tenant can see aggregate
activity counts. It does not expose content, identifiers or per-tenant breakdowns.
Validation errors omit the original input. Proxy/server logs require their own
retention and redaction configuration.

## Enterprise queue and persistence

Ingestion returns 202 and a durable job ID. The worker claims jobs in a SQLite
`BEGIN IMMEDIATE` transaction; expired 60-second leases are reclaimable. At most
three attempts are allowed. A fresh lease token fences stale workers, and each
batch commits documents, chunks, FTS entries and revision changes atomically.
Completed/failed job payloads are cleared. Job metadata has no automatic retention
purge. Documents have no automatic expiration or encrypted-at-rest layer.

Document IDs are upserts; repeated content is not duplicated. An idempotency key
is scoped to a tenant, and reusing it for a different batch returns 409. Order of
competing updates is completion order, not a guaranteed client sequence. Poll jobs
to completion before dependent updates/deletes. A previously queued upsert can
recreate a deleted document. Deletion removes active rows/index entries; it is not
secure erasure from WAL files, backups, prior responses or in-flight snapshots.

FAISS indexes are rebuilt lazily after a tenant revision changes, in embedding
batches of 128. Up to four tenant indexes are cached per process. This is exact
in-memory search; writes can make the next read expensive. Flat-vector memory is
roughly `chunks * dimension * 4` bytes plus text/metadata and temporary buffers.
There is no global memory quota or bounded durable-queue capacity. For sustained
large workloads add quotas, a dedicated queue, shared database and vector service.
No 100k-document production workload is claimed by the checked-in measurements.

Use SQLite's online backup API or a coordinated snapshot including WAL state;
copying only the main database file while writing can lose committed data. Test
restore procedures. Keep backups encrypted and define retention before real use.

## Release gates

The included CI runs lint, tests/evaluation on Windows and Linux, a container
readiness test, and a dependency audit. Before a real deployment, complete load,
failure-recovery, authorization, privacy and domain-specific validation. Healthcare
use additionally needs human governance and clinical/legal review. This project
does not supply medical advice or a regulatory compliance determination.
