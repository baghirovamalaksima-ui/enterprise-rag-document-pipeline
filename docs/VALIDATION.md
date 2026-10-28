# Executed validation — 28 October 2026

- Python 3.12.14, Windows 10, CPU only.
- **30 tests passed**, no test failures.
- Line coverage: **94.17%**. Optional local Hugging Face model execution is not covered.
- Ruff lint: passed.
- Docker Compose configuration parsed successfully; container build/runtime remain unverified.
- Real localhost HTTP smoke: uvicorn startup, readiness, OpenAPI, durable ingestion, worker completion and quoted answer passed; see [report](../artifacts/http-smoke.json).
- Runtime dependency audit: no known vulnerabilities reported by pip-audit at execution time.
- Evaluation: [raw report](../artifacts/evaluation.json), including dataset/code hash and per-case results.
- Test evidence: [log](../artifacts/tests.log), [JUnit XML](../artifacts/tests.xml), [coverage JSON](../artifacts/coverage.json).

The test environment emits one Starlette warning that the httpx-based TestClient
is deprecated in favor of httpx2. The existing tests execute successfully;
migration to the newer transport is a future compatibility task.

## What was not validated

- No paid/live OpenAI request was executed. HTTP contract, timeout, refusal,
  malformed JSON and quotation-validation tests use a mocked transport.
- No local neural embedding model was downloaded or executed. Default FAISS
  experiments use deterministic feature hashing, not learned semantic embeddings.
- Docker daemon was unavailable. Dockerfile and Compose are supplied; container
  build/runtime remain unverified locally. CI defines a build and readiness smoke test.
- GitHub Actions has not been observed running for these new repositories.
- No clinical validation, regulated-data processing, security certification,
  held-out quality evaluation, or monthly production throughput is established.

The scripts record actual outcomes; tests/evaluation must be rerun after code or
dependency changes. Timings describe one local run and are not service-level promises.
