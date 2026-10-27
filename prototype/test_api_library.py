import json,tempfile,threading,unittest,urllib.request,urllib.error
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from api_library import ApiKnowledge,validate_items,validate_filters,parts
from test_core import Embed,Vectors
from gateway import handler
from access import search_response
from test_rerank import client

def item(i='one',**metadata):return {'id':i,'text':'接口 /search 成功样本 '+i,'metadata':{'kb_domain':'test_api','doc_type':'testcase_chunk','source_table':'testcase','source_id':i,'source_hash':'old-source-'+i,'verified':True,'confidence':.9,'project':'学习工具','module':'拍题','env':'test','api_path':'/search','ordered_steps':[{'method':'POST','api_path':'/search'}],'evidence_refs':['source-run-'+i],**metadata}}

class Tests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.kb=ApiKnowledge(self.tmp.name,Embed(),Vectors(),'trusted')
 def tearDown(self):self.tmp.cleanup()
 def test_exact_metadata_original_id_typed_filter_and_idempotency(self):
  a=item(error_keywords=['timeout','断言失败'],param_name='token');result=self.kb.upsert([a]);self.assertEqual(result['count'],1);self.assertEqual(self.kb.item('one'),a)
  self.kb.filters=validate_filters({'project':'学习工具','verified':True,'env':'test'})
  r=search_response(self.kb.query('/search','trusted'),'test_api');self.assertEqual(r['results'][0]['id'],'one')
  self.assertEqual(r['results'][0]['metadata']['source_id'],'one');self.assertEqual(r['results'][0]['metadata']['ordered_steps'],a['metadata']['ordered_steps'])
  self.kb.embedding.fail=True;self.assertEqual(self.kb.upsert([a])['unchanged'],1)
  self.kb.filters=validate_filters({'param_name':'token','error_keywords':['timeout']});self.assertTrue(self.kb.query('/search','trusted')['evidence'])
  self.kb.filters=validate_filters({'error_keywords':'absent'});self.assertEqual(self.kb.query('/search','trusted')['evidence'],[])
  with self.assertRaises(ValueError):validate_filters({"x') OR 1=1 --":'attack'})
  self.kb.filters={'env':'stage'};self.assertEqual(self.kb.query('/search','trusted')['evidence'],[])
  self.assertEqual(self.kb.query('/search','other')['evidence'],[])
 def test_update_atomic_failure_and_current_version(self):
  self.kb.upsert([item('a'),item('b')]);old=self.kb.item('a')
  new=item('a');new['text']='新路径 /new';bad=item('b');bad['text']='FAIL_EMBED'
  with patch('api_library.time.sleep'):
   with self.assertRaises(RuntimeError):self.kb.upsert([new,bad])
  self.assertEqual(self.kb.item('a'),old)
  self.kb.upsert([new]);self.assertEqual(self.kb.item('a'),new)
  r=self.kb.query('/new','trusted');self.assertTrue(r['evidence']);self.assertTrue(all('/new' in e['text'] for e in r['evidence'] if e.get('original_id')=='a'))
 def test_validation_preserves_missing_verified_and_rejects_invalid(self):
  a=item();del a['metadata']['verified'];self.assertNotIn('verified',validate_items({'items':[a]})[0]['metadata'])
  for meta in [{'kb_domain':'app'},{'doc_type':'bug_card'},{'verified':'true'},{'confidence':float('nan')}]:
   with self.assertRaises(ValueError):validate_items({'items':[item(**meta)]})
  with self.assertRaises(ValueError):validate_filters({'verified':'true'})
  text='导航\n'+('x'*5800+'\n')*3;chunks=parts(text);self.assertEqual(''.join(c['text'] for c in chunks),text);self.assertTrue(all(len(c['text'])<=6000 for c in chunks))
 def test_model_time_withdrawal_and_verified_false_is_not_promoted(self):
  self.kb.upsert([item('a',verified=False),item('b')]);self.kb.reranker=client()
  r=self.kb.query('/search','trusted');self.assertEqual(r['ranking']['status'],'applied')
  self.assertFalse(next(e for e in r['evidence'] if e['original_id']=='a')['metadata']['verified'])
  def revoke(p):
   with self.kb.db() as c:c.execute("UPDATE publications SET state='withdrawn'")
   return {'results':[{'index':i,'relevance_score':.5} for i in range(len(p['documents']))]}
  self.kb.reranker.transport=revoke;self.assertEqual(self.kb.query('/search','trusted')['evidence'],[])
 def test_top50_keeps_rrf_tail_and_bounded_rerank(self):
  self.kb.upsert([item(str(i)) for i in range(55)])
  self.kb.reranker=client()
  result=self.kb.query('/search','trusted',50)
  self.assertEqual(len(result['evidence']),50)
  self.assertEqual(result['ranking']['submitted_count'],20)
  self.assertEqual(sum('rerank_score' in e for e in result['evidence']),20)
  self.kb.reranker.enabled=False
  self.assertEqual(len(self.kb.query('/search','trusted',50)['evidence']),50)
 def test_gateway_old_four_routes_tokens_and_batch_flat_order(self):
  cfg={'KB_GATEWAY_TOKEN':'read','KB_APP_WRITE_TOKEN':'app-write','KB_API_WRITE_TOKEN':'api-write','KB_COLLECTOR_PRINCIPAL':'trusted'}
  server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.kb,cfg));t=threading.Thread(target=server.serve_forever,daemon=True);t.start()
  def req(path,body,token='read'):
   r=urllib.request.Request('http://127.0.0.1:%s%s'%(server.server_port,path),data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
   try:
    with urllib.request.urlopen(r,timeout=10) as v:return v.status,json.load(v)
   except urllib.error.HTTPError as e:return e.code,json.load(e)
  try:
   def selected(cfg,domain,filters=None):self.kb.filters=filters or {};return self.kb
   with patch('gateway.knowledge',side_effect=selected),patch('gateway.configured_reranker',return_value=client()):
    code,r=req('/upsert',{'items':[item()]},'api-write');self.assertEqual(code,200);self.assertEqual(r['count'],1)
    self.assertEqual(req('/imports/api/items',{'items':[item()]})[0],403)
    self.assertEqual(req('/imports/api/items',{'items':[item()]},'app-write')[0],403)
    tasks=[{'query':'/search','top_k':50,'filters':{'kb_domain':'test_api','env':v}} for v in ['stage','test']]
    code,r=req('/batch_search',{'tasks':tasks});self.assertEqual(code,200);self.assertEqual(r['task_count'],2);self.assertEqual(len(r['results']),1)
    self.assertEqual(r['results'][0]['metadata']['source_id'],'one')
    # No enabled library route is configured in this fixture; fail closed.
    code,r=req('/rerank',{'query':'q','items':[{'text':'a'},{'text':'b'}]});self.assertEqual(code,403);self.assertEqual(r['error'],'rerank_not_enabled_for_library')
    self.assertEqual(req('/search',tasks[1])[0],200)
  finally:server.shutdown();server.server_close();t.join()
if __name__=='__main__':unittest.main()
