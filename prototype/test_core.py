import copy
import hashlib
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from core import Knowledge, IMAGE_COLLECTION
from parser import parse_document
from storage import FileObjects


class Embed:
    fail=False
    def embed_text(self,text):
        if self.fail or 'FAIL_EMBED' in text: raise RuntimeError('fake failure')
        return SimpleNamespace(vector=[1.,0.])
    def embed_image(self,data,mime):return SimpleNamespace(vector=[1.,0.])


class Vectors:
    def __init__(self):self.points={};self.fail_delete=False
    def upsert(self,cid,vector,source,version,collection=None):self.points[cid]=(source,version,collection)
    def exists(self,cid,collection=None):return cid in self.points
    def search(self,vector,versions,collection=None):
        return [cid for cid,(s,v,col) in self.points.items() if (s,v) in versions and col==collection]
    def delete_source(self,source):
        if self.fail_delete:raise RuntimeError('fake deletion failure')
        self.points={c:p for c,p in self.points.items() if p[0]!=source}
    def delete_ids(self,ids):
        if self.fail_delete:raise RuntimeError('fake deletion failure')
        self.points={c:p for c,p in self.points.items() if c not in ids}


def bundle(version='1',text='安全网关限制访问',principal='trusted'):
    doc={'raw_document':{'content':text},'chunks':[{'text':text,'block_id':'block1'}]}
    digest=hashlib.sha256(json.dumps(doc,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    now=time.time()
    return {'source_id':'source1','version':version,'title':'合成样本','url':'https://example.test/doc',
        'document':doc,'proof':{'principal':principal,'source_id':'source1','version':version,
        'view_allowed':True,'copy_allowed':True,'content_hash':digest,'verified_at':now,'expires_at':now+800}}


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.embed=Embed();self.vec=Vectors()
        self.kb=Knowledge(self.tmp.name,self.embed,self.vec,'trusted')
    def tearDown(self):self.tmp.cleanup()

    def ingest_public(self,b):
        result=self.kb.ingest(b)
        self.kb.publish({'source_id':b['source_id'],'version':b['version'],
            'content_hash':b['proof']['content_hash'],'audience':'common',
            'approved_by':'test-owner','approval_ref':'test-approval'})
        return result

    def test_identity_and_binding(self):
        for mutate in [lambda b:b['proof'].update(principal='other'),lambda b:b['proof'].update(version='2'),
                lambda b:b['proof'].update(source_id='other'),lambda b:b['proof'].update(content_hash='bad'),
                lambda b:b['proof'].update(copy_allowed=False),
                lambda b:b['proof'].update(expires_at=time.time()-1)]:
            b=bundle();mutate(b)
            with self.assertRaises(ValueError):self.kb.ingest(b)
        self.ingest_public(bundle());self.assertFalse(self.kb.query('安全','other')['evidence'])
        with self.kb.db() as c:
            c.execute("UPDATE publications SET content_hash='bad'")
        self.assertFalse(self.kb.query('安全','trusted')['evidence'])

    def test_atomic_version_and_idempotency(self):
        b=bundle();self.ingest_public(b);self.assertTrue(self.kb.ingest(b)['idempotent'])
        with self.assertRaises(RuntimeError):self.kb.ingest(bundle('2','FAIL_EMBED'))
        self.assertEqual(self.kb.authorized('trusted')[0]['version'],'1')
        self.kb.ingest(bundle('3','新版本安全'))
        self.assertEqual(self.kb.authorized('trusted')[0]['version'],'1')
        b=bundle('3','新版本安全')
        self.ingest_public(b)
        self.assertEqual(self.kb.authorized('trusted')[0]['version'],'3')
        with self.assertRaises(ValueError):self.kb.ingest(bundle('3','改变已发布内容'))

    def test_revoke_even_cleanup_failure(self):
        self.ingest_public(bundle());self.vec.fail_delete=True
        self.assertEqual(self.kb.revoke('source1')['cleanup'],'failed_retry_required')
        self.assertFalse(self.kb.query('安全','trusted')['evidence'])
        self.vec.fail_delete=False;self.assertEqual(self.kb.revoke('source1')['cleanup'],'done')

    def test_lexical_fallback(self):
        self.ingest_public(bundle());self.embed.fail=True
        r=self.kb.query('安全','trusted');self.assertTrue(r['evidence'])
        self.assertIn('dense_unavailable_using_lexical',r['warnings'])

    def test_revoke_during_query(self):
        self.ingest_public(bundle())
        original=self.embed.embed_text
        def revoke(text):self.kb.revoke('source1');return original(text)
        self.embed.embed_text=revoke
        self.assertFalse(self.kb.query('安全','trusted')['evidence'])

    def test_objects(self):
        ref=self.kb.objects.put(b'original');self.assertEqual(self.kb.objects.get(ref),b'original')
        with self.assertRaises(ValueError):self.kb.objects.get('../secret')
        path=self.kb.objects.root/ref[:2]/ref;path.write_bytes(b'tampered')
        with self.assertRaises(ValueError):self.kb.objects.get(ref)

    def test_media_storage_authorization(self):
        b=bundle();b['document']['media']=[{'data_base64':'YWJj','mime':'image/png','kind':'image',
                                          'coverage':'synthetic','block_id':'img1'}]
        b['proof']['content_hash']=hashlib.sha256(json.dumps(b['document'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        self.ingest_public(b)
        with self.kb.db() as c:aid=c.execute('SELECT asset_id FROM assets').fetchone()[0]
        self.assertEqual(self.kb.asset(aid,'trusted')[0],b'abc')
        with self.assertRaises(PermissionError):self.kb.asset(aid,'other')
        self.kb.revoke('source1')
        with self.assertRaises(PermissionError):self.kb.asset(aid,'trusted')

    def test_publication_independent_of_proof_expiry_and_restart(self):
        b=bundle();b['document']['media']=[{'data_base64':'YWJj','mime':'image/png','kind':'image',
                                          'coverage':'synthetic','block_id':'img1'}]
        b['proof']['content_hash']=hashlib.sha256(json.dumps(b['document'],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        self.kb.ingest(b)
        self.assertFalse(self.kb.query('安全','trusted')['evidence'])
        self.ingest_public(b)
        with self.kb.db() as c:
            proof=json.loads(c.execute('SELECT proof FROM versions').fetchone()[0]);proof['expires_at']=time.time()-2000
            c.execute('UPDATE versions SET proof=?',(json.dumps(proof),))
            aid=c.execute('SELECT asset_id FROM assets').fetchone()[0]
        self.kb=Knowledge(self.tmp.name,self.embed,self.vec,'trusted')
        self.assertTrue(self.kb.query('安全','trusted')['evidence'])
        self.assertTrue(self.kb.query_image(b'abc','image/png','trusted')['evidence'])
        self.assertEqual(self.kb.asset(aid,'trusted')[0],b'abc')
        with self.assertRaises(ValueError):self.kb.ingest({**b,'proof':proof})
        self.kb.unpublish('source1')
        self.assertFalse(self.kb.query('安全','trusted')['evidence'])
        self.assertFalse(self.kb.query_image(b'abc','image/png','trusted')['evidence'])
        with self.assertRaises(PermissionError):self.kb.asset(aid,'trusted')
        with self.kb.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM chunks').fetchone()[0],1)

    def test_unpublish_during_query(self):
        self.ingest_public(bundle());original=self.embed.embed_text
        def withdraw(text):self.kb.unpublish('source1');return original(text)
        self.embed.embed_text=withdraw
        self.assertFalse(self.kb.query('安全','trusted')['evidence'])

    def test_publication_requires_approval_and_complete_index(self):
        self.ingest_public(bundle());b=bundle('2','新版本安全');self.kb.ingest(b)
        req={'source_id':'source1','version':'2','content_hash':b['proof']['content_hash'],
             'audience':'common','approved_by':'owner','approval_ref':'ref'}
        for field in ['approved_by','approval_ref','content_hash']:
            broken={**req,field:''}
            with self.assertRaises(ValueError):self.kb.publish(broken)
        with self.assertRaises(ValueError):self.kb.publish({**req,'audience':'all_internet'})
        self.vec.points={cid:p for cid,p in self.vec.points.items() if p[1]!='2'}
        with self.assertRaises(ValueError):self.kb.publish(req)
        self.assertEqual(self.kb.authorized('trusted')[0]['version'],'1')

    def test_parser(self):
        d=parse_document({'content':'<h1 id="h">标题</h1><p id="p">正文<b>重点</b></p><table id="t"><tr><td>A</td><td>B</td></tr></table><whiteboard token="wb"/>'})
        self.assertEqual(len(d['chunks']),3);self.assertEqual(d['chunks'][2]['text'],'A | B')
        self.assertEqual(d['media'][0]['coverage'],'pending_download')
        self.assertEqual(parse_document({'content':'<img id="i" src="image-token"/>'})['media'][0]['token'],'image-token')
        self.assertTrue(parse_document({'content':'<sheet token="s"/>'})['unsupported'])
        with self.assertRaises(ValueError):parse_document({'content':'<!DOCTYPE x><p>x</p>'})

    def test_structured_text_without_hiding_resources(self):
        d=parse_document({'content':'<blockquote><p id="q">引用内容</p></blockquote><checkbox id="c" done="false">审核<cite type="user" user-name="人员"/></checkbox><cite type="doc" title="参考文档"/><readonly-block type="isv"/><task task-id="t"/>'})
        self.assertEqual([c['text'] for c in d['chunks']],['引用内容','待办：审核人员','参考文档'])
        self.assertEqual({x['kind'] for x in d['unsupported']},{'readonly-block','task'})


if __name__=='__main__':unittest.main()
