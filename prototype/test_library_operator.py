import json,tempfile,unittest,uuid
from pathlib import Path
from authorization import digest
from manage_credentials import write_atomic
from library_operator import provision,Denied,Conflict
from credential_vault import load_vault

class Tests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  for n,d in [('policy.json',{'version':1,'credentials':[]}),('library-routes.json',{'version':1,'libraries':[]}),('enrollment.json',{'version':1,'creators':[{'credential_id':'creator','token_digest':digest('create-secret'),'enabled':True,'domains':['app'],'max_libraries':2}]})]:write_atomic(self.root/n,d,0o600)
  self.body={'library_id':'new-app','project':'new_app','name':'新App','owner':'App负责人','kb_domain':'app','request_id':str(uuid.uuid4())}
 def tearDown(self):self.tmp.cleanup()
 def test_create_pair_replay_conflict_and_retention(self):
  a=provision(self.root,'create-secret',self.body);b=provision(self.root,'create-secret',self.body)
  self.assertFalse(a['replayed']);self.assertTrue(b['replayed']);self.assertEqual(a['read_token'],b['read_token']);self.assertEqual(a['write_token'],b['write_token']);self.assertFalse(a['rerank_enabled'])
  self.assertEqual(len(load_vault(self.root/'policy.json')['credentials']),2)
  changed={**self.body,'name':'changed'}
  with self.assertRaises(Conflict):provision(self.root,'create-secret',changed)
  with self.assertRaises(Conflict):provision(self.root,'create-secret',{**self.body,'request_id':str(uuid.uuid4())})
  with self.assertRaises(Denied):provision(self.root,a['write_token'],{**self.body,'library_id':'another'})
 def test_scope_cap_disabled_input(self):
  with self.assertRaises(Denied):provision(self.root,'wrong',self.body)
  with self.assertRaises(Denied):provision(self.root,'create-secret',{**self.body,'kb_domain':'test_api'})
  with self.assertRaises(ValueError):provision(self.root,'create-secret',{**self.body,'output_dir':'/tmp'})
  with self.assertRaises(ValueError):provision(self.root,'create-secret',{**self.body,'library_id':'..'})
  with self.assertRaises(ValueError):provision(self.root,'create-secret',{**self.body,'request_id':'predictable-id'})
  for i in range(2):provision(self.root,'create-secret',{**self.body,'library_id':'app'+str(i),'project':'project'+str(i),'request_id':str(uuid.uuid4())})
  with self.assertRaises(Denied):provision(self.root,'create-secret',{**self.body,'library_id':'third'})
  d=json.loads((self.root/'enrollment.json').read_text());d['creators'][0]['enabled']=False;write_atomic(self.root/'enrollment.json',d,0o600)
  with self.assertRaises(Denied):provision(self.root,'create-secret',self.body)
 def test_gateway_fixed_socket_request_no_policy_auth_required(self):
  from test_authorization import Permissions
  from unittest.mock import patch
  fixture=Permissions();fixture.setUp()
  try:
   with patch('library_operator.gateway_call',return_value=(201,{'ok':True,'read_token':'r','write_token':'w'})) as call:
    code,result=fixture.req('/libraries',self.body,'create-secret');self.assertEqual(code,201);self.assertTrue(result['ok']);self.assertEqual(call.call_args[0][0],'Bearer create-secret')
   self.assertEqual(fixture.req('/libraries',token='create-secret')[0],401) # creator is not a business reader
  finally:fixture.tearDown()
if __name__=='__main__':unittest.main()
