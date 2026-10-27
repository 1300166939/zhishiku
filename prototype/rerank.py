"""Bounded text reranking; safe errors and whole-batch RRF fallback."""
import json
import math
import queue
import threading
import time
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from diagnostics import event


class RerankError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RerankError('redirect_rejected')


class Reranker:
    def __init__(self, base_url, key, model='qwen3-rerank', enabled=False,
                 timeout=2, candidates=20, transport=None):
        parsed = urlsplit(base_url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Invalid rerank HTTPS base URL')
        self.url = base_url.rstrip('/') + '/v1/rerank'
        self.key, self.model, self.enabled = key, model, enabled
        self.timeout = min(10, max(.05, float(timeout)))
        self.candidates = min(30, max(2, int(candidates)))
        self.transport = transport or self._http
        # Shared per gateway: no queue, timed-out workers hold their slot until finished.
        self.slots = threading.BoundedSemaphore(2)
        self.lock = threading.Lock()
        self.failures = 0
        self.open_until = 0
        self.recovery_probe = False

    def _http(self, payload):
        req = urllib.request.Request(self.url, data=json.dumps(payload, ensure_ascii=False).encode(),
              headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=self.timeout) as response:
                raw = response.read(131073)
                if len(raw) > 131072:
                    raise RerankError('response_too_large')
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise RerankError('rate_limited' if exc.code == 429 else 'http_error') from None
        except RerankError:
            raise
        except Exception:
            raise RerankError('transport_error') from None

    @staticmethod
    def validate(response, count):
        results = response.get('results') if isinstance(response, dict) else None
        if not isinstance(results, list) or len(results) != count:
            raise RerankError('invalid_response')
        scores = {}
        for item in results:
            if not isinstance(item, dict):
                raise RerankError('invalid_response')
            index, score = item.get('index'), item.get('relevance_score')
            if (type(index) is not int or not 0 <= index < count or index in scores or
                type(score) not in (int, float) or not math.isfinite(score)):
                raise RerankError('invalid_response')
            scores[index] = score
        return scores

    def score(self, query, documents):
        now = time.monotonic()
        with self.lock:
            if now < self.open_until or self.recovery_probe:
                raise RerankError('circuit_open')
            probe = bool(self.open_until)
            if probe:
                self.recovery_probe = True
        if not self.slots.acquire(blocking=False):
            with self.lock:
                if probe:
                    self.recovery_probe = False
            raise RerankError('busy')
        result = queue.Queue(maxsize=1)
        payload = {'model': self.model, 'query': query, 'documents': documents,
                   'top_n': len(documents), 'return_documents': False}
        def worker():
            try:
                scores = self.validate(self.transport(payload), len(documents))
                result.put((scores, None))
            except RerankError as exc:
                result.put((None, exc.code))
            except Exception:
                result.put((None, 'transport_error'))
            finally:
                self.slots.release()
        threading.Thread(target=worker, daemon=True, name='kb-rerank').start()
        try:
            scores, error = result.get(timeout=self.timeout)
        except queue.Empty:
            scores, error = None, 'timeout'
        with self.lock:
            self.recovery_probe = False
            if error:
                self.failures += 1
                if self.failures >= 3:
                    self.open_until = time.monotonic() + 30
            else:
                self.failures = 0
                self.open_until = 0
        if error:
            raise RerankError(error)
        return scores


@lru_cache(maxsize=1)
def configured(path='/etc/ai-knowledge-base/rerank.env'):
    file = Path(path)
    if not file.exists():
        return None
    cfg = dict(line.split('=', 1) for line in file.read_text().splitlines() if '=' in line and not line.startswith('#'))
    return Reranker(cfg['KB_RERANK_BASE_URL'], cfg['KB_RERANK_API_KEY'],
                    cfg.get('KB_RERANK_MODEL', 'qwen3-rerank'),
                    cfg.get('KB_RERANK_ENABLED', 'false').lower() == 'true',
                    cfg.get('KB_RERANK_TIMEOUT_SECONDS', 2), cfg.get('KB_RERANK_CANDIDATES', 20))


def candidate_limit(client, top_k):
    return client.candidates if client and client.enabled else top_k


def rank(client, query, candidates, texts, top_k, trace_id):
    """Reorder text slots only. Image positions, IDs and RRF scores stay intact."""
    started = time.monotonic()
    metadata = {'strategy': 'rrf', 'status': 'disabled', 'candidate_count': len(candidates)}
    scores = {}
    warnings = []
    positions = [i for i, (cid, _, _) in enumerate(candidates) if cid in texts]
    if client and client.enabled:
        metadata.update(model=client.model, submitted_count=len(positions))
        if len(positions) < 2:
            metadata['status'] = 'skipped_empty' if not positions else 'skipped_single'
        else:
            # Never truncate stored/returned evidence, only the model input.
            per_document = min(1500, 30000 // len(positions))
            docs = [texts[candidates[i][0]][:per_document] for i in positions]
            metadata.update(input_characters=sum(map(len, docs)),
                            truncated_count=sum(len(texts[candidates[i][0]]) > per_document for i in positions))
            try:
                values = client.score(query, docs)
                scores = {candidates[pos][0]: values[i] for i, pos in enumerate(positions)}
                ranked = sorted((candidates[pos] for pos in positions), key=lambda c: -scores[c[0]])
                candidates = list(candidates)
                for pos, candidate in zip(positions, ranked):
                    candidates[pos] = candidate
                metadata.update(strategy='rrf+rerank', status='applied')
            except RerankError as exc:
                metadata['status'] = 'fallback_' + exc.code
                warnings.append('rerank_' + exc.code + '_using_rrf')
    metadata['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    event('rerank_complete', trace_id=trace_id, **metadata)
    return candidates[:top_k], scores, metadata, warnings
