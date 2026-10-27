# AI Knowledge Base

A single-node Python knowledge service with common, API and App libraries, project-scoped authorization, versioned ingestion and cited evidence retrieval.

## Architecture

- `prototype/gateway.py`: HTTP routing, authorization rechecks and query slots.
- `prototype/core.py`, `storage.py`, `parser.py`: source/version registry, immutable SHA256 objects, publication and parsing.
- `prototype/api_library.py`: structured API knowledge ingestion.
- `prototype/app_library.py`, `app_reports.py`, `app_maps.py`, `app_jobs.py`: App report/Map ingestion; screenshots remain URL references.
- `prototype/app_search.py`, `retrieval.py`, `rerank.py`: lexical/exact/dense retrieval, RRF and optional configured reranking.
- `prototype/evidence_qa.py`: `/ask` uses authorized FTS, neighboring sections, citations, conflict warnings and abstention. It makes no embedding/vector/rerank/generation calls.
- `prototype/governance.py`: explicit validity, authority references and supersession. Historical business validity defaults to unknown.
- `prototype/connector_queue.py`: manual durable queue, leases/retries/checkpoints and idempotent adapters for common/API/App. Web is unsupported.
- `prototype/architecture_ops.py`: checksum backups, isolated restores, text-vector rebuild and read-only GC plans.
- `prototype/authorization.py`, `library_routes.py`, `library_operator.py`: credential policy, library discovery and privileged self-service enrollment.
- `integration/`: official Ark embedding/vision adapters. No local GPU model is deployed.

SQLite WAL registries, filesystem objects, FTS and loopback Qdrant share one host. Independent request slots and a limited manual worker do not provide physical infrastructure isolation. Do not infer current business correctness from imported success records or source excerpts.

## Local verification

Python 3.11+ on Linux/macOS (the server uses `fcntl` and Linux service management).

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
PYTHONPATH=prototype:integration:deployment python -m unittest discover -s prototype -p 'test_*.py'
PYTHONPATH=prototype:integration:deployment python -m unittest discover -s integration -p 'test_*.py'
python tools/check_public_tree.py
```

Tests use synthetic fixtures and fake models/storage. Tests requiring original private HTML reports were removed during export. Do not copy those reports into this repository. `integration/smoke_ark.py` is an optional real-provider probe and requires your own configuration; it is not run by the test suite.

## Deployment

This is an implementation export, not a one-command production installer. Historical machine-specific deployment scripts and original data are intentionally excluded. See `deployment/README.md` and the example configuration files. The gateway binds to loopback; deploy your own HTTPS reverse proxy and private configuration, Qdrant API key, data directories and policy.

App report ingestion is restricted to the placeholder producer `https://reports.example.test/files/reports/`. Configure a real trusted producer by changing the exact allowlist consistently in `prototype/app_jobs.py` and `deployment/app_kb_client.py`; never replace it with an unrestricted URL regex.

## Security and operational boundaries

No company documents, database snapshots, credentials, production endpoints, report attachments or old Git history are included. Configuration examples contain names and empty values only. Never commit real `.env`, policy/vault files or runtime objects. Public placeholder URLs use reserved `.test` domains; official provider URLs and loopback addresses are intentionally retained.

Ingestion is manual. No collection timer is installed. GC only produces a plan; retention policy and offsite recovery require an explicit deployment decision. Before reverting to an old core without `qa_tokens`, remove `qa_insert`, `qa_delete`, `qa_update` triggers. Full/offsite recovery and business-quality acceptance are not implied by this export.

Offline export verification: 124 prototype tests and 5 integration tests passed on macOS. These are synthetic checks, not production load or business acceptance. The CI workflow repeats offline checks on Linux; its remote result is separate from local verification.

No open-source license is assigned by this export.
