import base64
import hashlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from access import ContractError, search_request, search_response
from app_library import AppKnowledge
from core import Knowledge
from gateway import handler
from test_core import Embed, Vectors, bundle


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.common = Knowledge(root/'common', Embed(), Vectors(), 'trusted')
        self.app = AppKnowledge(root/'app', Embed(), Vectors(), 'trusted')
        for kb, domain in [(self.common, 'common'), (self.app, 'app')]:
            b = bundle(text='登录失败执行记录')
            b['document']['media'] = [{'data_base64': 'YWJj', 'mime': 'image/png',
                'kind': 'before', 'coverage': 'sample', 'block_id': 'step-1'}]
            b['proof']['content_hash'] = hashlib.sha256(json.dumps(b['document'],
                ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            kb.ingest(b)
            kb.publish({'source_id': b['source_id'], 'version': b['version'],
                'content_hash': b['proof']['content_hash'], 'audience': domain,
                'approved_by': 'owner', 'approval_ref': 'ref'})
        pref = self.app.objects.put(json.dumps({'case_id': 'UI-1', 'run_id': 'r',
            'title': 'sample', 'steps': []}).encode())
        with self.app.db() as c:
            c.execute('INSERT INTO app_entities VALUES(?,?,?,?)', ('source1', '1',
                json.dumps({'entity_kind': 'step_evidence', 'case_id': 'UI-1',
                    'environment': 'test', 'run_id': 'r'}), pref))
        self.cfg = {'KB_GATEWAY_TOKEN': 'read-test', 'KB_APP_WRITE_TOKEN': 'write-test',
            'KB_COLLECTOR_PRINCIPAL': 'trusted', 'KB_APP_DATA_ROOT': str(root/'app')}
        def select(cfg, library='common', filters=None):
            if library == 'app':
                return AppKnowledge(root/'app', self.app.embedding, self.app.qdrant,
                    'trusted', filters=filters)
            if library == 'common' and filters is None:
                return self.common
            raise ValueError('Invalid route')
        self.mock = patch('gateway.knowledge', side_effect=select)
        self.mock.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.common, self.cfg))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.mock.stop(); self.tmp.cleanup()

    def request(self, path, body=None, token='read-test', raw=None):
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        if raw is not None:
            data = raw; headers['Content-Type'] = 'application/zip'
        else:
            data = json.dumps(body).encode() if body is not None else None
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(self.base+path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req) as r: return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_search_old_and_new_results_preserve_evidence(self):
        for domain in ['common', 'app']:
            old = self.request('/v1/agent-context/query', {'library': domain,
                'query': '登录失败', 'top_k': 5})[1]
            status, new = self.request('/search', {'query': '登录失败', 'top_k': 5,
                'filters': {'kb_domain': domain}})
            self.assertEqual(status, 200)
            self.assertEqual([e['evidence_id'] for e in old['evidence']],
                [e['id'] for e in new['results']])
            for before, after in zip(old['evidence'], new['results']):
                for key, value in before.items():
                    if key == 'metadata':
                        self.assertTrue(value.items() <= after[key].items())
                    else: self.assertEqual(after[key], value)
                self.assertEqual(after['metadata']['kb_domain'], domain)
            self.assertEqual(len(new['image_results']), len(old['image_evidence']))

    def test_domain_and_filter_errors_are_explicit(self):
        for body, error in [({'query': 'x'}, 'kb_domain_required'),
            ({'query': 'x', 'filters': {'kb_domain': 'unknown_domain'}}, 'unknown_kb_domain'),
            ({'query': 'x', 'filters': {'kb_domain': 'app', 'unknown_field': 'x'}}, 'unsupported_filter'),
            ({'query': 'x', 'filters': {'kb_domain': 'common', 'run_id': 'r'}}, 'unsupported_filter'),
            ({'query': 'x', 'filters': {'kb_domain': []}}, 'unknown_kb_domain'),
            ({'query': 'x', 'top_k': True, 'filters': {'kb_domain': 'app'}}, 'invalid_top_k')]:
            status, result = self.request('/search', body)
            self.assertEqual((status, result['error']), (400, error))
        _, result = self.request('/search', {'query': '登录', 'filters':
            {'kb_domain': 'app', 'case_id': 'other'}})
        self.assertEqual(result['results'], [])
        _, result = self.request('/search', {'query': '登录', 'filters':
            {'kb_domain': 'app', 'env': 'test'}})
        self.assertTrue(result['results'])

    def test_domains_auth_and_no_generic_writes(self):
        self.assertEqual(self.request('/domains', token=None)[0], 401)
        self.assertEqual(self.request('/search', {'query': 'x'}, token='invalid')[0], 401)
        status, result = self.request('/domains')
        self.assertEqual(status, 200)
        self.assertEqual([d['kb_domain'] for d in result['domains']], ['common','test_api', 'app'])
        self.assertEqual(result['dimension'], 2048)
        self.assertEqual(self.request('/upsert', {}, token='write-test')[0], 400)

    def test_image_search_keeps_original_asset(self):
        body = {'filters': {'kb_domain': 'app'}, 'image_base64': base64.b64encode(b'abc').decode(),
            'mime': 'image/png', 'top_k': 5}
        status, result = self.request('/search_image', body)
        self.assertEqual(status, 200); self.assertTrue(result['results'])
        ev = result['results'][0]
        self.assertEqual(ev['trust_status'], 'source_image')
        self.assertEqual(ev['asset_url'], '/v1/assets/app/' + ev['asset_id'])

    def test_package_job_aliases_and_read_write_boundary(self):
        url = 'https://reports.example.test/files/reports/sample/_summary.html'
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as z:
            z.writestr('report.html', '<html></html>')
            z.writestr('screenshots.json', '[]')
            z.writestr('metadata.json', json.dumps({'report_url': url, 'request_id': 'zip'}))
        self.assertEqual(self.request('/imports/app/packages', raw=stream.getvalue())[0], 403)
        self.assertEqual(self.request('/imports/app', {'report_url': url})[0], 403)
        self.assertEqual(self.request('/imports/app/packages', raw=b'bad', token='write-test')[0], 400)
        with patch('app_jobs.subprocess.Popen'):
            status, job = self.request('/imports/app/packages', raw=stream.getvalue(), token='write-test')
            self.assertEqual(status, 202)
            self.assertEqual(job['status_url'], '/imports/' + job['job_id'])
            self.assertEqual(self.request(job['status_url'])[1]['status'], 'queued')
            self.assertEqual(self.request('/v1/app/import-jobs/'+job['job_id'])[1]['job_id'], job['job_id'])
            status, other = self.request('/imports/app', {'report_url': url, 'request_id': 'url'}, token='write-test')
            self.assertEqual(status, 202); self.assertTrue(other['status_url'].startswith('/imports/'))

    def test_derived_metadata_is_not_lost(self):
        ev = {'evidence_id': 'e', 'text': '识别', 'score': 0.03, 'kind': 'text',
            'trust_status': 'model_derived', 'asset_id': 'a', 'image_sha256': 'hash',
            'source_version': '1', 'parent_context': {'run_id': 'r'}, 'recognition_ref': 'ref'}
        new = search_response({'evidence': [ev]}, 'app')['results'][0]
        for k, v in ev.items(): self.assertEqual(new[k], v)
        self.assertEqual(new['metadata']['kb_domain'], 'app')


if __name__ == '__main__': unittest.main()
