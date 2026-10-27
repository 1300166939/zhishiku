import json,tempfile,unittest
from pathlib import Path
from authorization import Policy,digest
from library_routes import load_routes,visible
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.path=self.root/'routes.json'
  self.catalog={'version':1,'libraries':[
   {'library_id':'app','name':'App全库','owner':'App负责人','kb_domain':'app','project':None,'enabled':True},
   {'library_id':'app-one','name':'项目一','owner':'项目负责人','kb_domain':'app','project':'one','enabled':True},
   {'library_id':'api','name':'API','owner':'API负责人','kb_domain':'test_api','project':None,'enabled':True},
   {'library_id':'common','name':'公共','owner':'团队','kb_domain':'common','project':None,'enabled':True}]}
 def tearDown(self):self.tmp.cleanup()
 def policy(self,grants):
  p=self.root/'policy.json';p.write_text(json.dumps({'version':1,'credentials':[{'credential_id':'reader','token_digest':digest('x'),'enabled':True,'grants':grants}]}))
  return Policy({'KB_AUTH_POLICY':str(p)})
 def test_reader_and_owner(self):
  p=self.policy([{'domain':d,'projects':'*','actions':['read']} for d in ['app','test_api','common']]);r=visible(p,'reader',self.catalog)
  self.assertEqual(r['mine'],[]);self.assertEqual(len(r['shared']),3);self.assertEqual(len(r['public']),1)
  self.assertEqual(r['private_readonly'],r['shared']);self.assertEqual(r['private_writable'],r['mine'])
  p=self.policy([{'domain':'app','projects':['one'],'actions':['read','create','update','import']}]);r=visible(p,'reader',self.catalog)
  self.assertEqual([i['library_id'] for i in r['mine']],['app-one']);self.assertFalse(r['shared']);self.assertEqual(r['mine'][0]['filters'],{'kb_domain':'app','project':'one'})
 def test_owner_description_is_not_authority(self):
  p=self.policy([{'domain':'app','projects':['one'],'actions':['read']}]);self.catalog['libraries'][1]['owner']='reader'
  r=visible(p,'reader',self.catalog);self.assertFalse(r['mine']);self.assertEqual(len(r['shared']),1)
 def test_public_token_cannot_discover_private(self):
  p=self.policy([{'domain':'common','projects':'*','actions':['read']}]);r=visible(p,'reader',self.catalog)
  self.assertEqual(len(r['public']),1);self.assertFalse(r['private_readonly']);self.assertFalse(r['private_writable'])
 def test_disabled_hidden_and_unknown_rejected(self):
  p=self.policy([{'domain':'app','projects':'*','actions':['read']}]);self.catalog['libraries'][0]['enabled']=False
  self.assertEqual(len(visible(p,'reader',self.catalog)['shared']),1)
  self.catalog['libraries'][1]['kb_domain']='unknown';self.path.write_text(json.dumps(self.catalog))
  with self.assertRaises(ValueError):load_routes(self.path)
 def test_duplicate_rejected(self):
  self.catalog['libraries'].append(dict(self.catalog['libraries'][0],library_id='duplicate'));self.path.write_text(json.dumps(self.catalog))
  with self.assertRaises(ValueError):load_routes(self.path)
if __name__=='__main__':unittest.main()
