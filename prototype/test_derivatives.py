import copy
import hashlib
import json
import unittest
from test_core import Tests as BaseTests, bundle
from derivatives import attach, rows


def recognition(image_hash,profile='a'*64):
    return {'recognition':{'image_type':'screenshot','visible_text':['ERR_LOGIN_TIMEOUT'],
        'summary':'界面显示登录超时','tables':[],'observed_status':[],'unreadable_regions':[]},
        'model':'verified-vision','profile':profile,'prompt_version':'v1','request_id':'request-test',
        'image_sha256':image_hash,'trust_status':'model_derived'}


class DerivativeTests(unittest.TestCase):
    setUp=BaseTests.setUp
    tearDown=BaseTests.tearDown
    ingest_public=BaseTests.ingest_public

    def prepare(self):
        b=bundle();b['document']['media']=[{'data_base64':'YWJj','mime':'image/png',
            'kind':'image','coverage':'synthetic','block_id':'img1'}]
        b['proof']['content_hash']=hashlib.sha256(json.dumps(b['document'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        self.ingest_public(b)
        with self.kb.db() as c:a=dict(c.execute('SELECT * FROM assets').fetchone())
        return {'asset_id':a['asset_id'],'image_sha256':a['object_ref'],'result':recognition(a['object_ref'])},b

    def test_provenance_cache_and_immutable_source(self):
        req,b=self.prepare();attach(self.kb,req)
        with self.kb.db() as c:
            self.assertEqual(c.execute('SELECT content_hash FROM versions').fetchone()[0],b['proof']['content_hash'])
            self.assertEqual(c.execute('SELECT count(*) FROM chunks').fetchone()[0],1)
        self.embed.fail=True
        self.assertTrue(attach(self.kb,req)['idempotent'])
        r=self.kb.query('ERR_LOGIN_TIMEOUT','trusted')
        derived=[x for x in r['evidence'] if x['trust_status']=='model_derived']
        self.assertTrue(derived);self.assertEqual(derived[0]['asset_id'],req['asset_id'])
        self.assertIn('#img1',derived[0]['source_url'])
        self.assertFalse(self.kb.query('ERR_LOGIN_TIMEOUT','other')['evidence'])
        broken=copy.deepcopy(req);broken['image_sha256']='0'*64
        with self.assertRaises(ValueError):attach(self.kb,broken)

    def test_failure_keeps_source_and_previous_derivatives(self):
        req,_=self.prepare();attach(self.kb,req);old={r['chunk_id'] for r in rows(self.kb)}
        req['result']=recognition(req['image_sha256'],'b'*64);self.embed.fail=True
        with self.assertRaises(RuntimeError):attach(self.kb,req)
        self.assertEqual({r['chunk_id'] for r in rows(self.kb)},old)
        self.assertTrue(self.kb.query('安全','trusted')['evidence'])
        self.embed.fail=False;attach(self.kb,req)
        self.assertNotEqual({r['chunk_id'] for r in rows(self.kb)},old)

    def test_unpublish_during_embedding_and_revoke_cleanup(self):
        req,_=self.prepare();attach(self.kb,req)
        original=self.embed.embed_text
        def withdraw(text):self.kb.unpublish('source1');return original(text)
        self.embed.embed_text=withdraw
        self.assertFalse(self.kb.query('ERR_LOGIN_TIMEOUT','trusted')['evidence'])
        with self.assertRaises(PermissionError):attach(self.kb,req)
        self.embed.embed_text=original;self.kb.revoke('source1')
        with self.kb.db() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM asset_derivatives').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM recognition_cache').fetchone()[0],0)

    def test_shared_cache_survives_other_source_revoke(self):
        req,b=self.prepare();attach(self.kb,req)
        b['source_id']='source2';b['proof']['source_id']='source2';self.ingest_public(b)
        with self.kb.db() as c:a=dict(c.execute("SELECT * FROM assets WHERE source_id='source2'").fetchone())
        req2={**req,'asset_id':a['asset_id']};self.assertTrue(attach(self.kb,req2)['cache_hit'])
        self.kb.revoke('source1')
        with self.kb.db() as c:
            cache=c.execute('SELECT object_ref FROM recognition_cache').fetchall()
            self.assertEqual(len(cache),1);self.kb.objects.get(cache[0][0])
        self.assertTrue(self.kb.query('ERR_LOGIN_TIMEOUT','trusted')['evidence'])

    def test_invalid_result_and_index_failure_stay_invisible(self):
        req,_=self.prepare();bad=copy.deepcopy(req)
        bad['result']['recognition']['tables']=[{'title':'t','columns':['a'],'rows':[[123]]}]
        with self.assertRaises(RuntimeError):attach(self.kb,bad)
        self.assertFalse(rows(self.kb))
        self.embed.fail=True
        with self.assertRaises(RuntimeError):attach(self.kb,req)
        self.assertFalse(rows(self.kb))
        self.assertTrue(self.kb.query('安全','trusted')['evidence'])
        self.embed.fail=False
        self.assertTrue(attach(self.kb,req)['cache_hit'])
        self.assertTrue(rows(self.kb))


del BaseTests

if __name__=='__main__':unittest.main()
