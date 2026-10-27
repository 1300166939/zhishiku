import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from core import Knowledge
from test_core import Embed,Vectors,bundle
from evidence_qa import build_index,query
from governance import register,resolve
from architecture_ops import backup_registry,restore_registry,rebuild_vectors,gc_plan
from connector_queue import Queue,validate

class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.kb=Knowledge(Path(self.tmp.name)/'kb',Embed(),Vectors(),'trusted')
    def tearDown(self):self.tmp.cleanup()
    def ingest(self,sid='old',version='1',text='示例周赛 周结算规则',heading='Product/示例周赛/v1/01-prd/rules.md'):
        b=bundle(version,text);b['source_id']=sid;b['url']='https://example.test/Product/示例周赛/v1/01-prd/rules.md';b['proof']['source_id']=sid
        b['document']['chunks'][0]['heading_path']=heading
        b['proof']['content_hash']=hashlib.sha256(json.dumps(b['document'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        self.kb.ingest(b);self.kb.publish({'source_id':sid,'version':version,'content_hash':b['proof']['content_hash'],'audience':'common','approved_by':'test','approval_ref':'fixture'})
        return b
    def test_index_manual_then_citations(self):
        self.ingest();self.assertEqual(query(self.kb,'示例周赛','trusted')['abstain_reason'],'qa_index_not_ready')
        build_index(self.kb);r=query(self.kb,'示例周赛','trusted')
        self.assertTrue(r['claims']);self.assertEqual(r['claims'][0]['citation_ids'],['E1'])
        self.assertIn('business_validity_unconfirmed',r['warnings'])
        self.assertEqual(r['image_evidence'],[])
    def test_trigger_new_ingest(self):
        build_index(self.kb);self.ingest();self.assertTrue(query(self.kb,'示例周赛','trusted')['evidence'])
        self.ingest('other','2','示例周赛 新规则');self.assertEqual(build_index(self.kb)['indexed_chunks'],2)
    def test_authorization_and_revocation(self):
        self.ingest();build_index(self.kb)
        self.assertFalse(query(self.kb,'示例周赛','other')['evidence'])
        self.kb.unpublish('old');self.assertFalse(query(self.kb,'示例周赛','trusted')['evidence'])
    def test_successor_explicit_and_valid(self):
        self.ingest();self.ingest('new','2')
        m={'state':'approved','family':'rules','authority_ref':'fixture','supersedes':[{'source_id':'old','version':'1'}]}
        register(self.kb,'new','2',m,'admin','confirmed fixture')
        self.assertEqual(set(resolve(self.kb,self.kb.authorized('trusted'))),{('new','2')})
        self.assertFalse(register(self.kb,'new','2',m,'admin','same')['changed'])
        self.kb.unpublish('new');self.assertIn(('old','1'),resolve(self.kb,self.kb.authorized('trusted')))
    def test_expired_successor_does_not_suppress(self):
        self.ingest();self.ingest('new','2')
        register(self.kb,'new','2',{'state':'approved','authority_ref':'fixture','valid_until':time.time()-10,'supersedes':[{'source_id':'old','version':'1'}]},'admin','expired fixture')
        self.assertEqual(set(resolve(self.kb,self.kb.authorized('trusted'))),{('old','1')})
    def test_unconfirmed_successor_and_cycles(self):
        self.ingest();self.ingest('new','2')
        register(self.kb,'new','2',{'supersedes':[{'source_id':'old','version':'1'}]},'admin','unknown')
        self.assertEqual(len(resolve(self.kb,self.kb.authorized('trusted'))),2)
        with self.assertRaises(ValueError):register(self.kb,'old','1',{'supersedes':[{'source_id':'new','version':'2'}]},'admin','cycle')
        with self.assertRaises(ValueError):register(self.kb,'new','2',{'state':'approved'},'admin','no evidence')
    def test_feature_priority(self):
        self.ingest();self.ingest('feature','1','示例周赛 核心用例 周结算验收','Product/示例周赛/v1/02-feature-list/feature-list.md')
        build_index(self.kb);r=query(self.kb,'示例周赛核心用例','trusted',1)
        self.assertEqual(r['evidence'][0]['source_id'],'feature')
    def test_backup_restore_rebuild_and_gc(self):
        self.ingest();build_index(self.kb)
        backup=Path(self.tmp.name)/'backup';backup_registry(self.kb,backup)
        restored=Path(self.tmp.name)/'restored';self.assertEqual(restore_registry(backup,restored)['registry'],'PASS')
        second=Knowledge(restored,Embed(),Vectors(),'trusted')
        self.assertEqual(rebuild_vectors(second,'trusted')['text_vectors'],1)
        self.assertTrue(query(second,'示例周赛','trusted')['evidence'])
        orphan=self.kb.objects.put(b'orphan');self.assertIn(orphan,gc_plan(self.kb)['unreferenced_objects'])
        with self.assertRaises(ValueError):restore_registry(backup,restored)
        (backup/'registry.sqlite3').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):restore_registry(backup,Path(self.tmp.name)/'bad')
    def test_queue_idempotency_retry_and_lease(self):
        b=self.ingest();e={'schema_version':1,'connector':'common_bundle','source_id':'old','version':'1','request_id':'r1','payload':{'bundle':b}}
        q=Queue(Path(self.tmp.name)/'q');q.submit(e);self.assertTrue(q.submit(e)['idempotent'])
        with self.assertRaises(ValueError):q.submit({**e,'version':'2','payload':{'bundle':{**b,'version':'2'}}})
        def fail(e):raise RuntimeError('secret must not be persisted')
        self.assertEqual(q.run_one({'common_bundle':fail},now=100)['status'],'retry')
        self.assertEqual(q.run_one({'common_bundle':lambda e:{'ok':True}},now=101)['status'],'idle')
        self.assertEqual(q.run_one({'common_bundle':lambda e:{'ok':True}},now=103)['status'],'done')
        with q.db() as c:
            c.execute("UPDATE connector_jobs SET status='running',lease_until=104")
        self.assertEqual(q.run_one({'common_bundle':lambda e:{'ok':True}},now=105)['status'],'done')
        with q.db() as c:
            self.assertEqual(c.execute('SELECT version FROM connector_checkpoints').fetchone()[0],'1')
            self.assertGreater(c.execute('SELECT count(*) FROM connector_events').fetchone()[0],3)
    def test_unsupported_web_and_secret(self):
        b=self.ingest();e={'schema_version':1,'connector':'common_bundle','source_id':'old','version':'1','request_id':'r','payload':{'bundle':b}}
        with self.assertRaises(ValueError):validate({**e,'connector':'web'})
        with self.assertRaises(ValueError):validate({**e,'payload':{'token':'test'}})
    def test_unknown_subject_does_not_match_generic_tail(self):
        self.ingest(text='用户报名资格与竞赛积分');build_index(self.kb)
        r=query(self.kb,'火星殖民基地核聚变竞赛报名资格','trusted')
        self.assertFalse(r['evidence']);self.assertEqual(r['abstain_reason'],'insufficient_subject_evidence')
    def test_three_real_local_adapters(self):
        from unittest.mock import patch
        from connector_queue import adapters
        from api_library import ApiKnowledge
        from app_library import AppKnowledge
        from test_app_maps import MapEmbed
        from test_api_library import item
        from app_maps import worker,status
        api=ApiKnowledge(Path(self.tmp.name)/'api',Embed(),Vectors(),'trusted')
        app=AppKnowledge(Path(self.tmp.name)/'app',MapEmbed(),Vectors(),'trusted')
        cfg={'KB_COLLECTOR_PRINCIPAL':'trusted','KB_APP_DATA_ROOT':str(app.root)}
        b=self.ingest()
        pub={'source_id':'old','version':'1','content_hash':b['proof']['content_hash'],'audience':'common','approved_by':'admin','approval_ref':'fixture'}
        with patch('gateway.knowledge',side_effect=lambda cfg,d='common':{'common':self.kb,'test_api':api,'app':app}[d]),patch('app_maps.spawn'):
            a=adapters(cfg)
            self.assertTrue(a['common_bundle']({'payload':{'bundle':b,'publication':pub}})['idempotent'])
            e={'request_id':'api-adapter','payload':{'items':[item()]}}
            self.assertEqual(a['api_items'](e)['count'],1);self.assertEqual(a['api_items'](e)['unchanged'],1)
            m={'request_id':'map-adapter','payload':{'request_id':'map-adapter','items':[{'id':'m1','text':'示例入口规则','metadata':{'project':'one','title':'示例'}}]}}
            self.assertEqual(a['app_maps'](m)['published'],1)
            self.assertEqual(a['app_maps'](m)['unchanged'],1)
    def test_same_family_candidates_warn_without_assuming_latest(self):
        self.ingest('old','1',heading='Product/示例周赛/v1/01-prd/示例周赛_V1.0.md')
        self.ingest('new','2',heading='Product/示例周赛/v1/01-prd/示例周赛_V1.1.md')
        build_index(self.kb);r=query(self.kb,'示例周赛','trusted')
        self.assertTrue(r['conflicts']);self.assertEqual(len(r['evidence']),1)

if __name__=='__main__':unittest.main()
