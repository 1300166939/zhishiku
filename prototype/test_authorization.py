"""Adversarial authorization HTTP and atomic publication tests."""
import contextlib,copy,io,json,tempfile,threading,unittest,urllib.request,urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from authorization import Policy,Forbidden,digest,compatibility,validate_policy,report_context,api_contexts
from gateway import handler
from core import Knowledge
from app_library import AppKnowledge
from api_library import ApiKnowledge,validate_items
from app_maps import validate,import_items,worker,status
from test_core import Vectors
from test_app_links import TextEmbed,DEP
from app_reports import parse_report
from import_app import import_report
class Permissions(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.cfg={'KB_GATEWAY_TOKEN':'read','KB_APP_WRITE_TOKEN':'app-old','KB_API_WRITE_TOKEN':'api-old','KB_COLLECTOR_PRINCIPAL':'trusted','KB_APP_DATA_ROOT':str(self.root/'app'),'KB_API_DATA_ROOT':str(self.root/'api'),'KB_AUTH_POLICY':str(self.root/'policy.json')};self.policy=compatibility(self.cfg)
  for cid,domain,actions in [('app-one','app',['read','create','update','import']),('api-one','test_api',['read','create','update']),('app-reader','app',['read']),('api-reader','test_api',['read'])]:self.policy['credentials'].append({'credential_id':cid,'token_digest':digest(cid),'enabled':True,'grants':[{'domain':domain,'projects':['one'],'actions':actions}]})
  self.save();self.ap=AppKnowledge(self.root/'app',TextEmbed(),Vectors(),'trusted');self.api=ApiKnowledge(self.root/'api',TextEmbed(),Vectors(),'trusted');self.common=Knowledge(self.root/'common',TextEmbed(),Vectors(),'trusted')
  for project in ['one','two']:
   items,_=validate({'items':[self.mi(project)]});import_items(self.ap,items,'seed');self.api.upsert(validate_items({'items':[self.ai(project)]}))
  self.mock=patch('gateway.knowledge',side_effect=self.select);self.mock.start();self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.common,self.cfg));self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.mock.stop();self.tmp.cleanup()
 def save(self):self.policy['version']+=1;Path(self.cfg['KB_AUTH_POLICY']).write_text(json.dumps(validate_policy(self.policy)))
 def disable(self,cid):next(c for c in self.policy['credentials'] if c['credential_id']==cid)['enabled']=False;self.save()
 def select(self,cfg,library='common',filters=None):
  if library=='common':return self.common
  if library=='app':return AppKnowledge(self.root/'app',self.ap.embedding,self.ap.qdrant,'trusted',filters=filters)
  return ApiKnowledge(self.root/'api',self.api.embedding,self.api.qdrant,'trusted',filters=filters)
 def mi(self,project='one',iid='entry'):return {'id':iid,'text':'历史记录入口','metadata':{'project':project,'title':'历史记录'}}
 def test_ask_preserves_project_scope_and_revocation(self):
  from evidence_qa import build_index
  build_index(self.ap);build_index(self.api)
  for token,domain in [('app-reader','app'),('api-reader','test_api')]:
   code,r=self.req('/ask',{'query':'历史记录','filters':{'kb_domain':domain}},token)
   self.assertEqual(code,200);self.assertTrue(r['evidence'])
   allowed={(v['source_id'],v['version']) for v in self.select(self.cfg,domain,{'project':'one'}).authorized('trusted')}
   self.assertTrue(all((e['source_id'],e['source_version']) in allowed for e in r['evidence']))
   self.assertEqual(self.req('/ask',{'query':'历史记录','filters':{'kb_domain':domain,'project':'two'}},token)[0],403)
   self.disable(token);self.assertEqual(self.req('/ask',{'query':'历史记录','filters':{'kb_domain':domain}},token)[0],401)
 def ai(self,project='one',iid=None):return {'id':iid or project,'text':'历史记录接口 /history','metadata':{'project':project,'doc_type':'testcase_chunk','kb_domain':'test_api','verified':True}}
 def req(self,path,body=None,token='app-one'):
  req=urllib.request.Request('http://127.0.0.1:'+str(self.server.server_port)+path,data=json.dumps(body).encode() if body is not None else None,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
  try:
   with urllib.request.urlopen(req,timeout=10) as r:return r.status,json.load(r)
  except urllib.error.HTTPError as e:return e.code,json.load(e)
 def test_projects_before_dense_and_listing(self):
  for domain,token in [('app','app-one'),('test_api','api-one')]:
   code,d=self.req('/search',{'query':'历史','filters':{'kb_domain':domain}},token);self.assertEqual(code,200);self.assertTrue(d['results']);self.assertTrue(all(r['metadata']['project']=='one' for r in d['results']));self.assertEqual(self.req('/search',{'query':'历史','filters':{'kb_domain':domain,'project':['one','two']}},token)[0],403)
  self.assertEqual(self.req('/maps/app',{})[1]['total'],1)
 def test_alias_batch_cross_domain_no_model(self):
  self.assertEqual(self.req('/v1/agent-context/query',{'library':'test_api','query':'历史'})[0],403)
  tasks=[{'query':'历史','filters':{'kb_domain':d}} for d in ['app','test_api']]
  with patch.object(TextEmbed,'embed_text',side_effect=AssertionError('No model')):self.assertEqual(self.req('/batch_search',{'tasks':tasks})[0],403)
 def test_identity_and_disable(self):
  code,d=self.req('/auth/me');self.assertEqual(code,200);self.assertNotIn('token_digest',json.dumps(d));self.disable('app-one');self.assertEqual(self.req('/auth/me')[0],401)
 def test_library_catalog_authorized_and_failure_isolated(self):
  path=self.root/'routes.json';self.cfg['KB_LIBRARY_ROUTES']=str(path)
  catalog={'version':1,'libraries':[{'library_id':'app-one','name':'App一','owner':'App负责人','kb_domain':'app','project':'one','enabled':True},{'library_id':'app-two','name':'App二','owner':'App负责人','kb_domain':'app','project':'two','enabled':True}]}
  path.write_text(json.dumps(catalog));code,d=self.req('/libraries');self.assertEqual(code,200);self.assertEqual([r['library_id'] for r in d['mine']],['app-one']);self.assertFalse(d['shared'])
  code,d=self.req('/libraries',token='app-reader');self.assertEqual(code,200);self.assertFalse(d['mine']);self.assertEqual(len(d['shared']),1)
  self.assertEqual(self.req('/libraries',token='invalid')[0],401)
  path.write_text('broken');self.assertEqual(self.req('/libraries')[0],503)
  self.assertEqual(self.req('/search',{'query':'历史','filters':{'kb_domain':'app'}})[0],200)
 def test_writes_aliases_mixed_batches_denied(self):
  for token in ['app-reader','read']:
   for path in ['/upsert','/imports/app/maps','/imports/app','/imports/app/packages','/v1/app/import-jobs','/v1/app/report-packages']:self.assertEqual(self.req(path,{'items':[self.mi()]},token)[0],403)
  for path in ['/upsert','/imports/api/items']:self.assertEqual(self.req(path,{'items':[self.ai()]})[0],403)
  with patch('app_maps.spawn') as spawn:self.assertEqual(self.req('/upsert',{'items':[self.mi('one'),self.mi('two')]} )[0],403);spawn.assert_not_called()
 def test_direct_rerank_new_denied_existing_app_and_api_preserved(self):
  path=self.root/'routes.json';self.cfg['KB_LIBRARY_ROUTES']=str(path)
  catalog={'version':1,'libraries':[{'library_id':d,'name':d,'owner':'test','kb_domain':d,'project':'one','enabled':True,'rerank_enabled':True} for d in ['app','test_api']]}
  path.write_text(json.dumps(catalog))
  for token in ['app-one','api-one','app-reader','api-reader']:
   self.assertEqual(self.req('/rerank',{'query':'历史','items':[]},token)[0],200)
  catalog['libraries'][0]['rerank_enabled']=False;path.write_text(json.dumps(catalog))
  with patch('gateway.configured_reranker',side_effect=AssertionError('disabled provider')):
   code,res=self.req('/rerank',{'query':'历史','items':[{'text':'历史'}]},'app-one')
   self.assertEqual(code,403);self.assertEqual(res['error'],'rerank_not_enabled_for_library')
 def test_api_stored_project_immutable(self):
  with patch.object(TextEmbed,'embed_text',side_effect=AssertionError('No model')):self.assertEqual(self.req('/upsert',{'items':[self.ai('one','two')]},'api-one')[0],403)
  self.assertEqual(self.api.item('two')['metadata']['project'],'two');self.assertEqual(self.req('/upsert',{'items':[self.ai('two','one')]},'api-old')[0],403)
 def test_api_revoke_commit_preserves_old(self):
  items=validate_items({'items':[self.ai()]});items[0]['text']='new';p=Policy(self.cfg);api_contexts(p,'api-one',self.api,items);self.disable('api-one')
  with self.assertRaises(Forbidden):self.api.upsert(items,commit_guard=lambda:p.commit(api_contexts(p,'api-one',self.api,items)))
  self.assertIn('/history',self.api.item('one')['text'])
 def test_map_job_read_scope_and_revocation(self):
  with patch('app_maps.spawn'):code,job=self.req('/imports/app/maps',{'items':[self.mi('one','new')],'request_id':'job-one'})
  self.assertEqual(code,202);self.assertEqual(self.req(job['status_url'],token='api-one')[0],403);self.assertEqual(self.req(job['status_url'],token='app-reader')[0],200);self.disable('app-one');worker(self.cfg,job['job_id']);self.assertEqual(status(self.cfg,job['job_id'])['status'],'failed');self.assertEqual(self.req('/maps/app',{},'app-reader')[1]['total'],1)
 def test_legacy_jobs_project_read_denied(self):
  with patch('app_jobs.subprocess.Popen'):code,job=self.req('/imports/app',{'report_url':'https://reports.example.test/files/reports/legacy/_summary.html'},'app-old')
  self.assertEqual(code,202);self.assertEqual(self.req(job['status_url'],token='app-reader')[0],403);self.assertEqual(self.req(job['status_url'],token='read')[0],200)
 def test_asset_domain_denied(self):self.assertEqual(self.req('/v1/assets/app/fake',token='api-one')[0],403)
 def test_invalid_policy(self):
  bad=copy.deepcopy(self.policy);bad['credentials'][0]['grants'][0]['actions']=['delete']
  with self.assertRaises(ValueError):validate_policy(bad)
  Path(self.cfg['KB_AUTH_POLICY']).write_text('invalid')
  self.assertEqual(self.req('/auth/me')[0],503)
  self.assertEqual(self.req('/search',{'query':'历史','filters':{'kb_domain':'app'}})[0],503)
 def test_map_revoke_during_index(self):
  items,_=validate({'items':[self.mi('one','revoke')]});p=Policy(self.cfg);ctx=p.context('app-one','app','import',['one']);original=self.ap.qdrant.exists
  def revoke(cid):self.disable('app-one');return original(cid)
  with patch.object(self.ap.qdrant,'exists',side_effect=revoke),self.assertRaises(Forbidden):import_items(self.ap,items,'revoke',p,ctx)
  with self.ap.db() as c:self.assertEqual(c.execute("SELECT count(*) FROM publications WHERE approved_by='gateway-map-token:revoke'").fetchone()[0],0)
 def test_create_only_and_update_only(self):
  cred=next(c for c in self.policy['credentials'] if c['credential_id']=='api-one');cred['grants'][0]['actions']=['read','create'];self.save()
  self.assertEqual(self.req('/upsert',{'items':[self.ai()]},'api-one')[0],403)
  self.assertEqual(self.req('/upsert',{'items':[self.ai('one','new')]},'api-one')[0],200)
  cred['grants'][0]['actions']=['read','update'];self.save()
  self.assertEqual(self.req('/upsert',{'items':[self.ai('one','another')]},'api-one')[0],403)
  self.assertEqual(self.req('/upsert',{'items':[self.ai('one','new')]},'api-one')[0],200)
 def test_policy_cli_atomic_changes(self):
  from manage_credentials import change
  p=Policy(self.cfg);p.path.chmod(0o640);f=self.root/'issued.env';g=[{'domain':'app','projects':['one'],'actions':['read']}]
  change(p.path,'issue','issued',g,f);self.assertEqual(f.stat().st_mode&0o777,0o600);self.assertEqual(p.authenticate('Bearer '+f.read_text().strip().split('=',1)[1]),'issued')
  change(p.path,'disable','issued');self.assertIsNone(p.authenticate('Bearer '+f.read_text().strip().split('=',1)[1]))
  self.assertEqual(p.path.stat().st_mode&0o777,0o640)
  with self.assertRaises(ValueError):change(p.path,'issue','issued',g,self.root/'other.env')
 def test_midquery_scope_narrow_fails(self):
  orig=self.ap.embedding.embed_text
  def narrow(text):next(c for c in self.policy['credentials'] if c['credential_id']=='app-one')['grants'][0]['projects']=['two'];self.save();return orig(text)
  with patch.object(self.ap.embedding,'embed_text',side_effect=narrow):self.assertEqual(self.req('/search',{'query':'历史','filters':{'kb_domain':'app'}})[0],403)
if __name__=='__main__':unittest.main()
