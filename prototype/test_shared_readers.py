import json,tempfile,unittest
from pathlib import Path
from authorization import digest
from manage_credentials import change,write_atomic
from shared_readers import load_registry,export
class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/'policy.json'
  write_atomic(self.path,{'version':1,'credentials':[]},0o640)
 def tearDown(self):self.tmp.cleanup()
 def policy(self):return json.loads(self.path.read_text())
 def test_issue_rotate_disable_scope(self):
  grants=[{'domain':'app','projects':['one'],'actions':['read']}];file=self.root/'token.env'
  change(self.path,'issue','reader',grants,file,share_read=True,label='共享项目',default_read=True)
  first=export(self.policy(),load_registry(self.path));self.assertEqual(first['credentials'][0]['token'],file.read_text().strip().split('=',1)[1]);self.assertTrue(first['credentials'][0]['active']);self.assertEqual(self.path.stat().st_mode&0o777,0o640)
  file2=self.root/'rotated.env';change(self.path,'rotate','reader',token_file=file2);second=export(self.policy(),load_registry(self.path));self.assertNotEqual(first['credentials'][0]['token'],second['credentials'][0]['token']);self.assertTrue(second['credentials'][0]['default'])
  change(self.path,'disable','reader');self.assertNotIn('token',export(self.policy(),load_registry(self.path))['credentials'][0])
  change(self.path,'enable','reader');self.assertTrue(export(self.policy(),load_registry(self.path))['credentials'][0]['active'])
  change(self.path,'grant','reader',[{'domain':'app','projects':['two'],'actions':['read']}]);self.assertEqual(export(self.policy(),load_registry(self.path))['credentials'][0]['grants'][0]['projects'],['two'])
 def test_write_cannot_be_published(self):
  with self.assertRaises(ValueError):change(self.path,'issue','writer',[{'domain':'app','projects':'*','actions':['read','create']}],self.root/'writer.env',share_read=True)
  self.assertEqual(self.policy()['credentials'],[]);self.assertFalse((self.root/'writer.env').exists())
 def test_elevation_or_digest_mismatch_suppresses_token(self):
  file=self.root/'token.env';change(self.path,'issue','reader',[{'domain':'app','projects':'*','actions':['read']}],file,share_read=True)
  change(self.path,'grant','reader',[{'domain':'app','projects':'*','actions':['read','create']}]);self.assertNotIn('token',export(self.policy(),load_registry(self.path))['credentials'][0])
  p=self.policy();p['credentials'][0]['grants'][0]['actions']=['read'];p['credentials'][0]['token_digest']=digest('different');self.assertNotIn('token',export(p,load_registry(self.path))['credentials'][0])
 def test_unregistered_issue_is_private(self):
  change(self.path,'issue','private',[{'domain':'app','projects':'*','actions':['read']}],self.root/'token.env');self.assertEqual(load_registry(self.path)['credentials'],[])
if __name__=='__main__':unittest.main()
