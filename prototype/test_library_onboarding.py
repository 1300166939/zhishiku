import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from authorization import Policy
from register_private_library import create
from library_routes import load_routes,rerank_allowed,credential_rerank_allowed
from shared_readers import export,load_registry
from manage_credentials import write_atomic
from credential_vault import load_vault,vault_path,libraries
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.policy=self.root/'policy.json';self.routes=self.root/'routes.json'
  write_atomic(self.policy,{'version':1,'credentials':[]},0o640);write_atomic(self.routes,{'version':1,'libraries':[]},0o640)
 def tearDown(self):self.tmp.cleanup()
 def create(self):return create(self.policy,self.routes,'other-app','other_app','其他App','负责人',self.root/'tokens')
 def test_create_read_write_default_disabled_and_conflict(self):
  result=self.create();p=Policy({'KB_AUTH_POLICY':str(self.policy)});r=load_routes(self.routes)
  self.assertFalse(rerank_allowed(r,'app','other_app'));self.assertFalse(credential_rerank_allowed(p,'other-app-reader',r,'app'))
  self.assertEqual(p.scope('other-app-reader','app','read')[0],['other_app']);self.assertEqual(p.scope('other-app-writer','app','import')[0],['other_app'])
  self.assertEqual(len(export(p.load(),load_registry(self.policy))['credentials']),1)
  self.assertTrue(all(Path(f).stat().st_mode&0o777==0o600 for f in result['token_files']));self.assertEqual(self.policy.stat().st_mode&0o777,0o640)
  v=load_vault(self.policy);self.assertEqual(len(v['credentials']),2);self.assertEqual(vault_path(self.policy).stat().st_mode&0o777,0o600)
  tokens=libraries(p.load(),v,r,'other-app',True)['libraries'][0]
  for role,index in [('read',0),('write',1)]:self.assertEqual(tokens[role]['token'],Path(result['token_files'][index]).read_text().strip().split('=',1)[1])
  self.assertNotIn('token',libraries(p.load(),v,r)['libraries'][0]['read'])
  with self.assertRaises(ValueError):self.create()
 def test_failure_rolls_back_all(self):
  original=write_atomic
  def fail(path,data,mode=None):
   if Path(path)==self.policy and data['version']>1:raise OSError('test')
   return original(path,data,mode)
  with patch('register_private_library.write_atomic',side_effect=fail),self.assertRaises(OSError):self.create()
  self.assertFalse(load_routes(self.routes)['libraries']);self.assertFalse(load_registry(self.policy)['credentials']);self.assertFalse(list((self.root/'tokens').glob('*.env')))
  self.assertFalse(vault_path(self.policy).exists())
 def test_rotation_retained_disabled_status_and_stale_not_shown(self):
  from manage_credentials import change
  self.create();cid='other-app-reader';output=self.root/'rotated.env';change(self.policy,'rotate',cid,token_file=output)
  p=Policy({'KB_AUTH_POLICY':str(self.policy)});row=libraries(p.load(),load_vault(self.policy),load_routes(self.routes),'other-app',True)['libraries'][0]
  self.assertEqual(row['read']['token'],output.read_text().strip().split('=',1)[1])
  change(self.policy,'disable',cid);row=libraries(p.load(),load_vault(self.policy),load_routes(self.routes),'other-app',True)['libraries'][0]
  self.assertFalse(row['read']['active']);self.assertTrue(row['read']['saved'])
  data=p.load();data['credentials'][0]['token_digest']='a'*64
  row=libraries(data,load_vault(self.policy),load_routes(self.routes),'other-app',True)['libraries'][0]
  self.assertEqual(row['read']['status'],'stale');self.assertNotIn('token',row['read'])
 def test_default_cloud_output_and_insecure_vault_rejected(self):
  result=create(self.policy,self.routes,'default-app','default_app','默认App','负责人')
  self.assertTrue(all(Path(p).parent==self.root/'issued/default-app' for p in result['token_files']))
  vault_path(self.policy).chmod(0o644)
  with self.assertRaises(ValueError):load_vault(self.policy)
 def test_project_override_beats_domain_and_unknown_disabled(self):
  catalog={'libraries':[{'kb_domain':'app','project':None,'enabled':True,'rerank_enabled':True,'legacy_projects':['old',None]},{'kb_domain':'app','project':'old','enabled':True,'rerank_enabled':False}]}
  self.assertFalse(rerank_allowed(catalog,'app','old'));self.assertTrue(rerank_allowed(catalog,'app',None));self.assertFalse(rerank_allowed(catalog,'app','new'))
if __name__=='__main__':unittest.main()
