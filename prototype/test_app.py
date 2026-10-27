import json
import tempfile
import unittest
from pathlib import Path
from app_reports import parse_report
from app_library import AppKnowledge,validate_filters
from core import Qdrant
from test_core import Embed,Vectors,bundle
from import_app import chunk_fields,report_version

class AppTests(unittest.TestCase):


    def test_collection_and_filter_isolation(self):
        q=Qdrant('test',library='app');calls=[]
        q.request=lambda path,*args: calls.append(path) or {'id':'x'}
        q.upsert('x',[1.], 's','v');q.exists('x');q.delete_source('s');q.delete_ids(['x'])
        self.assertTrue(all('kb_app_' in x for x in calls))
        with tempfile.TemporaryDirectory() as root:
            kb=AppKnowledge(root,Embed(),Vectors(),'trusted');b=bundle();kb.ingest(b)
            req={'source_id':'source1','version':'1','content_hash':b['proof']['content_hash'],'audience':'app','approved_by':'owner','approval_ref':'user'}
            with self.assertRaises(ValueError):kb.publish({**req,'audience':'common'})
            kb.publish(req)
            pref=kb.objects.put(json.dumps({'case_id':'UI-1','run_id':'r','title':'sample','review_status':'unreviewed','steps':[]}).encode())
            with kb.db() as c:c.execute('INSERT INTO app_entities VALUES(?,?,?,?)',('source1','1',json.dumps({'entity_kind':'case_run_summary','case_id':'UI-1','run_id':'r','reported_status':'failed','review_status':'unreviewed'}),pref))
            kb.filters={'case_id':'other'};self.assertEqual(kb.query('安全','trusted')['evidence'],[])
            kb.filters={'case_id':'UI-1'};ev=kb.query('安全','trusted')['evidence'][0]
            self.assertEqual(ev['library'],'app');self.assertNotIn('#',ev['source_url'])
            self.assertEqual(ev['parent_context']['run_id'],'r')
            kb.unpublish('source1');self.assertEqual(kb.reports()['counts']['runs'],0)

    def test_app_withdraw_preserves_manifest_image(self):
        import base64,hashlib
        from derivatives import attach
        from test_derivatives import recognition
        with tempfile.TemporaryDirectory() as root:
            kb=AppKnowledge(root,Embed(),Vectors(),'trusted');b=bundle()
            b['document']['media']=[{'data_base64':'YWJj','mime':'image/png','kind':'before','coverage':'sample','block_id':'step-1'}]
            b['proof']['content_hash']=hashlib.sha256(json.dumps(b['document'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            kb.ingest(b);kb.publish({'source_id':'source1','version':'1','content_hash':b['proof']['content_hash'],'audience':'app','approved_by':'owner','approval_ref':'ref'})
            with kb.db() as c:
                asset=dict(c.execute('SELECT * FROM assets').fetchone())
                c.execute('INSERT INTO app_downloads VALUES(?,?,?,?,?)',('https://source.test/image',asset['object_ref'],'image/png','downloaded',None))
                c.execute('INSERT INTO app_entities VALUES(?,?,?,?)',('source1','1',json.dumps({'entity_kind':'case_run_summary','project':'synthetic'}),kb.objects.put(b'{}')))
            req={'asset_id':asset['asset_id'],'image_sha256':asset['object_ref'],'result':recognition(asset['object_ref'])}
            attach(kb,req);self.assertTrue(attach(kb,req)['idempotent'])
            self.assertEqual(kb.revoke('source1')['cleanup'],'done')
            self.assertEqual(kb.objects.get(asset['object_ref']),b'abc')
            with self.assertRaises(PermissionError):kb.asset(asset['asset_id'],'trusted')

    def test_no_unknown_filters_or_overflow(self):
        with self.assertRaises(ValueError):validate_filters({'kb_domain':'API'})
        with self.assertRaises(ValueError):validate_filters({'run_id':[]})
        chunks=chunk_fields(['x'*13000],'s');self.assertGreater(len(chunks),5)
        self.assertTrue(all(len(c['text'])<=1200 for c in chunks))
        self.assertEqual(''.join(c['text'].replace('\n','') for c in chunks),'x'*13000)

if __name__=='__main__':unittest.main()
