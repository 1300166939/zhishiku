"""Map API isolation, atomicity, optimistic updates and unchanged model reuse."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from app_library import AppKnowledge,MAP_TYPE,REPORT_TYPE
from app_maps import validate,import_items,listing,submit,status,MapConflict,parts
from gateway import handler
from test_core import Embed,Vectors

class MapEmbed(Embed):
    model='doubao-embedding-vision-251215';dimensions=2048
    def embed_image(self,*a,**kw):raise AssertionError('No image model allowed')

def item(iid='entry',project='test_map',text='首页\n→ 示例功能\n→ 拍题记录',**meta):
    return {'id':iid,'text':text,'metadata':{'project':project,'title':'拍题历史入口',**meta}}

class MapTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.embed=MapEmbed();self.vector=Vectors()
        self.kb=AppKnowledge(self.tmp.name,self.embed,self.vector,'trusted')
    def tearDown(self):self.tmp.cleanup()
    def load(self,items,jid='test'):
        self.kb.embedding=self.embed
        normalized,_=validate({'items':items})
        return import_items(self.kb,normalized,jid)
    def test_import_query_and_links_no_image_data(self):
        result=self.load([item(image_urls=['https://example.test/entry.png'],source_url='https://example.test/map')])
        self.assertEqual(result['model_calls'],1)
        found=self.kb.query('示例功能','trusted')['evidence'][0]
        self.assertEqual(found['metadata']['doc_type'],MAP_TYPE)
        self.assertEqual(found['trust_status'],'author_provided_map')
        self.assertEqual(found['screenshots'][0]['asset_url'],'https://example.test/entry.png')
        self.assertEqual(found['source_locator']['item_id'],'entry')
        with self.kb.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM assets').fetchone()[0],0)
    def test_identical_import_no_model_calls_and_stable_version(self):
        first=self.load([item()]);again=self.load([item()])
        self.assertEqual(again['model_calls'],0);self.assertEqual(again['unchanged'],1)
        self.assertEqual(first['sources'][0]['version'],again['sources'][0]['version'])
    def test_update_if_match_and_old_version_excluded(self):
        first=self.load([item()]);updated=item(text='我的书房 → 示例功能 → 历史记录')
        updated['if_match']=first['sources'][0]['version']
        result=self.load([updated]);self.assertNotEqual(result['sources'][0]['version'],updated['if_match'])
        with self.assertRaises(MapConflict):self.load([updated],jid='new-stale-request')
        self.assertEqual([v['version'] for v in self.kb.authorized('trusted')],[result['sources'][0]['version']])
    def test_batch_failure_preserves_every_previous_source(self):
        self.load([item(),item('second',text='原规则')])
        old={(v['source_id'],v['version']) for v in self.kb.authorized('trusted')}
        with patch('import_app.time.sleep'):
            with self.assertRaises(RuntimeError):self.load([item(text='新导航规则'),item('second',text='FAIL_EMBED')])
        self.assertEqual(old,{(v['source_id'],v['version']) for v in self.kb.authorized('trusted')})
    def test_worker_resume_after_commit_before_job_status(self):
        before=self.load([item()])
        update=item(text='新的导航入口');update['if_match']=before['sources'][0]['version']
        self.load([update],jid='interrupted-job')
        resumed=self.load([update],jid='interrupted-job')
        self.assertEqual(resumed['unchanged'],1);self.assertEqual(resumed['model_calls'],0)
        with self.assertRaises(MapConflict):self.load([update],jid='different-job')
    def test_project_type_scalar_list_and_listing(self):
        self.load([item(project='one'),item(project='two')])
        self.kb.filters={'project':'one','doc_type':[MAP_TYPE]}
        found=self.kb.query('示例功能','trusted')['evidence']
        self.assertEqual(len(found),1);self.assertEqual(found[0]['metadata']['project'],'one')
        self.kb.filters={'doc_type':REPORT_TYPE};self.assertEqual(self.kb.authorized('trusted'),[])
        self.kb.filters={};result=listing(self.kb,{'filters':{'project':['one','two']},'limit':1})
        self.assertEqual(result['total'],2);self.assertEqual(result['next_offset'],1)
        self.assertEqual(self.kb.reports()['counts']['runs'],0)
    def test_input_rejects_missing_project_trust_other_domain_and_duplicate(self):
        for items in [[item(project='')],[item(trust_status='confirmed')],[item(kb_domain='common')],[item(doc_type=REPORT_TYPE)],[item(),item()]]:
            with self.assertRaises(ValueError):validate({'items':items})
    def test_id_namespace_no_colon_collision(self):
        result=self.load([item('b:c',project='a'),item('c',project='a:b')])
        self.assertEqual(len(set(s['source_id'] for s in result['sources'])),2)
    def test_navigation_chain_and_headings_retained(self):
        chunks=parts(item(text='# 进入页面\n首页\n→ 示例功能\n→ 历史记录\n\n# 返回\n点击返回'))
        self.assertEqual(len(chunks),2);self.assertIn('首页\n→ 示例功能\n→ 历史记录',chunks[0]['text'])
        self.assertEqual(chunks[1]['heading_path'],'返回')
    def test_request_id_persisted_content_conflict(self):
        cfg={'KB_APP_DATA_ROOT':self.tmp.name}
        with patch('app_maps.spawn'):
            first=submit(cfg,{'items':[item()],'request_id':'same'})
            again=submit(cfg,{'items':[item()],'request_id':'same'})
            self.assertEqual(first['job_id'],again['job_id']);self.assertTrue(again['idempotent_request'])
            with self.assertRaises(MapConflict):submit(cfg,{'items':[item(text='changed')],'request_id':'same'})
            self.assertEqual(status(cfg,first['job_id'])['kind'],'app_map')
    def test_http_write_permissions_async_contract_search_filter_and_status(self):
        cfg={'KB_APP_DATA_ROOT':self.tmp.name,'KB_GATEWAY_TOKEN':'read','KB_APP_WRITE_TOKEN':'write','KB_COLLECTOR_PRINCIPAL':'trusted'}
        self.load([item()])
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.kb,cfg))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,body=None,token='read'):
            req=urllib.request.Request('http://127.0.0.1:%s'%server.server_port+path,data=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            try:
                with urllib.request.urlopen(req,timeout=5) as r:return r.status,json.load(r)
            except urllib.error.HTTPError as e:return e.code,json.load(e)
        try:
            with patch('gateway.knowledge',return_value=self.kb),patch('app_maps.spawn'):
                self.assertEqual(request('/upsert',{'items':[item()]})[0],403)
                code,job=request('/upsert',{'items':[item()]},'write');self.assertEqual(code,202)
                self.assertEqual(request(job['status_url'])[1]['kind'],'app_map')
                code,body=request('/search',{'query':'示例功能','top_k':5,'filters':{'kb_domain':'app','project':'test_map','doc_type':MAP_TYPE}})
                self.assertEqual(code,200);self.assertEqual(body['results'][0]['metadata']['doc_type'],MAP_TYPE)
                self.assertEqual(request('/maps/app',{'filters':{}})[1]['total'],1)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
