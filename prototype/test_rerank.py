"""Ranking identity, bounded HTTP failure, publication races and compatible APIs."""
import copy
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from rerank import Reranker, RerankError, rank, candidate_limit
from core import Knowledge
from app_library import AppKnowledge, MAP_TYPE
from app_maps import import_items, validate
from test_core import Embed, Vectors, bundle
from test_app_maps import MapEmbed, item
from access import search_response


def reverse(payload):
    return {'results': [{'index': i, 'relevance_score': i / 10}
                        for i in reversed(range(len(payload['documents'])))]}


def client(transport=reverse, **options):
    return Reranker('https://provider.test', 'test-secret', enabled=True, transport=transport, **options)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [('a', .04, ['bm25']), ('image', .03, ['image_dense']), ('b', .02, ['dense'])]
        self.texts = {'a': 'A' * 3000, 'b': 'B' * 3000}

    def test_scores_reorder_only_text_preserve_rrf_and_image_slots(self):
        calls = []
        def transport(p):
            calls.append(p)
            return reverse(p)
        candidates, scores, meta, warnings = rank(client(transport), 'question', self.candidates, self.texts, 3, 'trace')
        self.assertEqual([c[0] for c in candidates], ['b', 'image', 'a'])
        self.assertEqual(candidates[0][1:], self.candidates[2][1:])
        self.assertEqual(meta['status'], 'applied')
        self.assertEqual(meta['truncated_count'], 2)
        self.assertEqual(max(map(len, calls[0]['documents'])), 1500)
        self.assertEqual(warnings, [])

    def test_invalid_partial_duplicate_bool_nan_and_out_of_range_all_fallback(self):
        bad = [None, {}, {'results': []}, {'results': [{'index': 0, 'relevance_score': .5}]},
               {'results': [{'index': 0, 'relevance_score': .5}, {'index': 0, 'relevance_score': .3}]},
               {'results': [{'index': 2, 'relevance_score': .5}, {'index': 1, 'relevance_score': .3}]},
               {'results': [{'index': True, 'relevance_score': .5}, {'index': 0, 'relevance_score': .3}]},
               {'results': [{'index': 0, 'relevance_score': float('nan')}, {'index': 1, 'relevance_score': .3}]},
               {'results': [{'index': 0, 'relevance_score': True}, {'index': 1, 'relevance_score': .3}]}]
        for response in bad:
            with self.subTest(response=response):
                result = rank(client(lambda p: response), 'q', self.candidates, self.texts, 3, 't')
                self.assertEqual(result[0], self.candidates)
                self.assertEqual(result[1], {})
                self.assertEqual(result[2]['status'], 'fallback_invalid_response')

    def test_disabled_empty_single_and_stable_ties(self):
        def fail(p): raise AssertionError('must skip')
        off = client(fail); off.enabled = False
        self.assertEqual(candidate_limit(off, 5), 5)
        self.assertEqual(rank(off, 'q', self.candidates, self.texts, 3, 't')[2]['status'], 'disabled')
        self.assertEqual(rank(client(fail), 'q', [], {}, 3, 't')[2]['status'], 'skipped_empty')
        self.assertEqual(rank(client(fail), 'q', self.candidates[:1], self.texts, 3, 't')[2]['status'], 'skipped_single')
        def tie(p): return {'results': [{'index': i, 'relevance_score': .5} for i in reversed(range(len(p['documents'])))]}
        self.assertEqual(rank(client(tie), 'q', self.candidates, self.texts, 3, 't')[0], self.candidates)

    def test_timeout_is_wall_clock_two_slots_no_queue_and_no_early_release(self):
        gate = threading.Event(); entered = threading.Barrier(3)
        def blocked(p):
            entered.wait(timeout=1); gate.wait(timeout=2); return reverse(p)
        c = client(blocked, timeout=.08); outcomes = []
        def run():
            start = time.monotonic()
            outcomes.append((rank(c, 'q', self.candidates, self.texts, 3, 't'), time.monotonic() - start))
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads: t.start()
        entered.wait(timeout=1)
        self.assertEqual(rank(c, 'q', self.candidates, self.texts, 3, 't')[2]['status'], 'fallback_busy')
        for t in threads: t.join()
        self.assertTrue(all(r[0][2]['status'] == 'fallback_timeout' and r[1] < .3 for r in outcomes))
        self.assertEqual(rank(c, 'q', self.candidates, self.texts, 3, 't')[2]['status'], 'fallback_busy')
        gate.set()
        for _ in range(100):
            if c.slots.acquire(blocking=False): c.slots.release(); break
            time.sleep(.005)

    def test_three_failures_open_circuit_and_one_recovery_probe(self):
        def failed(p): raise RerankError('rate_limited')
        c = client(failed)
        for _ in range(3): self.assertEqual(rank(c, 'q', self.candidates, self.texts, 3, 't')[2]['status'], 'fallback_rate_limited')
        self.assertEqual(rank(c, 'q', self.candidates, self.texts, 3, 't')[2]['status'], 'fallback_circuit_open')
        c.open_until = time.monotonic() - 1; c.transport = reverse
        self.assertEqual(rank(c, 'q', self.candidates, self.texts, 3, 't')[2]['status'], 'applied')

    def test_http_contract_auth_body_errors_redirect_and_bounded_response(self):
        seen = []; mode = ['ok']
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append((self.path, self.headers['Authorization'], data))
                if mode[0] == 'redirect':
                    self.send_response(302); self.send_header('Location', '/another'); self.end_headers(); return
                if mode[0] == '429': self.send_response(429); self.end_headers(); return
                self.send_response(200); self.end_headers()
                self.wfile.write(b'x' * 131073 if mode[0] == 'large' else json.dumps(reverse(data)).encode())
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
        c = client(); c.transport = c._http
        c.url = 'http://127.0.0.1:%s/v1/rerank' % server.server_port  # test server only
        try:
            self.assertEqual(rank(c, 'q', self.candidates, self.texts, 3, 't')[2]['status'], 'applied')
            self.assertEqual(seen[0][0:2], ('/v1/rerank', 'Bearer test-secret'))
            self.assertNotIn('messages', seen[0][2])
            for mode_value, expected in [('429', 'rate_limited'), ('redirect', 'redirect_rejected'), ('large', 'response_too_large')]:
                mode[0] = mode_value
                result = rank(c, 'q', self.candidates, self.texts, 3, 't')
                self.assertEqual(result[0], self.candidates)
                self.assertEqual(result[2]['status'], 'fallback_' + expected)
            self.assertEqual(len(seen), 4)  # redirect not followed, no automatic retries
        finally: server.shutdown(); server.server_close(); t.join()


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kb = AppKnowledge(self.tmp.name, MapEmbed(), Vectors(), 'trusted')
        values, _ = validate({'items': [item(str(i), text='示例功能历史入口选项 ' + str(i),
                                  image_urls=['https://example.test/%s.png' % i]) for i in range(8)]})
        import_items(self.kb, values, 'test')

    def tearDown(self): self.tmp.cleanup()

    def test_pool_beyond_old_topk_mapping_and_no_embedding_reimport(self):
        baseline = self.kb.query('示例功能', 'trusted', 5)
        seen = []
        def transport(p): seen.append(p); return reverse(p)
        self.kb.reranker = client(transport)
        result = self.kb.query('示例功能', 'trusted', 5)
        self.assertEqual(len(seen[0]['documents']), 8)
        self.assertNotIn(result['evidence'][0]['evidence_id'], [e['evidence_id'] for e in baseline['evidence']])
        self.assertEqual(result['ranking']['status'], 'applied')
        for ev in result['evidence']:
            self.assertIn(ev['metadata']['item_id'] + '.png', ev['screenshots'][0]['asset_url'])
            self.assertIn('rerank_score', ev)
        adapted = search_response(result, 'app')
        self.assertEqual(adapted['score_type'], 'rrf')
        self.assertEqual(adapted['ranking'], result['ranking'])
        self.assertEqual(adapted['results'][0]['screenshots'], result['evidence'][0]['screenshots'])

    def test_fallback_identical_baseline_ids_scores_and_disabled(self):
        baseline = self.kb.query('示例功能', 'trusted')
        self.kb.reranker = client(lambda p: {})
        fallback = self.kb.query('示例功能', 'trusted')
        self.assertEqual(fallback['evidence'], baseline['evidence'])
        self.kb.reranker.enabled = False
        self.assertEqual(self.kb.query('示例功能', 'trusted')['evidence'], baseline['evidence'])

    def test_new_library_skips_provider_and_retains_rrf(self):
        baseline=self.kb.query('示例功能','trusted',5)
        def fail(p):raise AssertionError('Disabled project reached provider')
        self.kb.reranker=client(fail);self.kb.rerank_project_gate=lambda p:False
        result=self.kb.query('示例功能','trusted',5)
        self.assertEqual(result['evidence'],baseline['evidence'])
        self.assertEqual(result['ranking']['status'],'disabled')
        self.assertEqual(result['ranking']['library_disabled_count'],8)

    def test_mixed_libraries_only_enabled_texts_reorder_in_their_slots(self):
        values,_=validate({'items':[item('foreign',project='new_app',text='NEW_PROJECT_SENTINEL 示例功能')]})
        import_items(self.kb,values,'new')
        self.kb.max_top_k=9  # Inspect all fixture slots without altering the HTTP limit.
        baseline=self.kb.query('示例功能','trusted',9)
        seen=[]
        def inspect(p):seen.extend(p['documents']);return reverse(p)
        self.kb.reranker=client(inspect);self.kb.rerank_project_gate=lambda p:p=='test_map'
        result=self.kb.query('示例功能','trusted',9)
        self.assertEqual(len(seen),8);self.assertNotIn('NEW_PROJECT_SENTINEL',' '.join(seen))
        old_slot=next(i for i,e in enumerate(baseline['evidence']) if e['metadata']['project']=='new_app')
        self.assertEqual(result['evidence'][old_slot]['evidence_id'],baseline['evidence'][old_slot]['evidence_id'])
        self.assertEqual(result['ranking']['status'],'applied')
        self.assertEqual(result['ranking']['library_disabled_count'],1)

    def test_app_withdraw_during_model_never_returns_and_no_untrusted_send(self):
        def withdraw(p):
            with self.kb.db() as c: c.execute("UPDATE publications SET state='withdrawn'")
            return reverse(p)
        self.kb.reranker = client(withdraw)
        self.assertEqual(self.kb.query('示例功能', 'other')['evidence'], [])
        result = self.kb.query('示例功能', 'trusted')
        self.assertEqual(result['evidence'], [])
        self.assertEqual(result['abstain_reason'], 'no_authorized_evidence')

    def test_common_text_rerank_and_model_time_publication_recheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            kb = Knowledge(tmp, Embed(), Vectors(), 'trusted')
            for i in range(3):
                b = bundle(text='导航历史 ' + str(i)); b['source_id'] = b['proof']['source_id'] = 'source' + str(i)
                kb.ingest(b); kb.publish({'source_id': b['source_id'], 'version': '1', 'content_hash': b['proof']['content_hash'],
                    'audience': 'common', 'approved_by': 'owner', 'approval_ref': 'test'})
            baseline = kb.query('导航', 'trusted')
            kb.reranker = client()
            result = kb.query('导航', 'trusted')
            self.assertEqual(result['ranking']['status'], 'applied')
            self.assertEqual(result['evidence'][0]['evidence_id'], baseline['evidence'][-1]['evidence_id'])
            def withdraw(p):
                with kb.db() as c: c.execute("UPDATE publications SET state='withdrawn'")
                return reverse(p)
            kb.reranker.transport = withdraw
            self.assertEqual(kb.query('导航', 'trusted')['evidence'], [])

    def test_scoped_candidates_exclude_other_project_and_old_version_before_send(self):
        values,_=validate({'items':[item('foreign',project='other',text='FOREIGN_SENTINEL 示例功能'),
                                   item('0',text='OLD_SENTINEL 示例功能')]})
        import_items(self.kb,values,'old')
        values,_=validate({'items':[item('0',text='CURRENT_SENTINEL 示例功能')]})
        import_items(self.kb,values,'new')
        seen=[]
        def inspect(p):seen.extend(p['documents']);return reverse(p)
        self.kb.filters={'project':'test_map'};self.kb.reranker=client(inspect)
        self.kb.query('示例功能','trusted')
        self.assertTrue(seen)
        self.assertNotIn('FOREIGN_SENTINEL',' '.join(seen))
        self.assertNotIn('OLD_SENTINEL',' '.join(seen))

    def test_common_derived_ready_and_original_image_preserved_then_superseded(self):
        from test_derivatives import DerivativeTests
        from derivatives import attach,rows
        fixture=DerivativeTests();fixture.setUp()
        try:
            req,_=fixture.prepare();attach(fixture.kb,req)
            seen=[]
            def inspect(p):seen.extend(p['documents']);return reverse(p)
            fixture.kb.reranker=client(inspect)
            result=fixture.kb.query('ERR_LOGIN_TIMEOUT','trusted',8)
            self.assertEqual(result['ranking']['status'],'applied')
            derived=[e for e in result['evidence'] if e['trust_status']=='model_derived']
            self.assertTrue(derived);self.assertTrue(result['image_evidence'])
            self.assertEqual(derived[0]['asset_id'],req['asset_id'])
            self.assertTrue(any('ERR_LOGIN_TIMEOUT' in text for text in seen))
            def invalidate(p):
                with fixture.kb.db() as c:c.execute("UPDATE asset_derivatives SET status='superseded'")
                return reverse(p)
            fixture.kb.reranker.transport=invalidate
            result=fixture.kb.query('ERR_LOGIN_TIMEOUT','trusted',8)
            self.assertFalse([e for e in result['evidence'] if e['trust_status']=='model_derived'])
        finally:fixture.tearDown()

    def test_version_changes_during_model_never_return_old_app_evidence(self):
        def switch(p):
            with self.kb.db() as c:c.execute("UPDATE sources SET active_version='future-version'")
            return reverse(p)
        self.kb.reranker=client(switch)
        self.assertEqual(self.kb.query('示例功能','trusted')['evidence'],[])

    def test_report_screenshot_run_step_identity_after_rank(self):
        from test_app_links import LinksTests, TextEmbed
        from import_app import import_report
        raw, report = LinksTests().fixture()
        report['runs'][0]['steps'][1]['action'] = '查询拍题历史'
        with tempfile.TemporaryDirectory() as tmp:
            kb = AppKnowledge(tmp, TextEmbed(), Vectors(), 'trusted')
            import_report(kb, report, {}, raw, workers=1)
            kb.filters = {'entity_kind': 'step_evidence'}
            kb.reranker = client()
            result = kb.query('步骤', 'trusted')
            self.assertEqual(result['ranking']['status'], 'applied')
            for ev in result['evidence']:
                meta = ev['metadata']
                step = next(s for s in report['runs'][0]['steps'] if s['step_index'] == meta['step_index'])
                self.assertEqual([(s['role'], s['asset_url']) for s in ev['screenshots']], [(s['role'], s['url']) for s in step['screenshots']])
                self.assertTrue(all(s['run_id'] == meta['run_id'] and s['step_index'] == meta['step_index'] for s in ev['screenshots']))


if __name__ == '__main__': unittest.main()
