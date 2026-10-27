"""Lexical fallback, current publication, filter isolation and bounded HTTP admission."""
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from app_library import AppKnowledge,MAP_TYPE,REPORT_TYPE
from app_maps import validate,import_items,listing
from app_search import backfill
from test_core import Vectors
from test_app_maps import MapEmbed,item
from gateway import handler

class OptimizedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.embed=MapEmbed();self.kb=AppKnowledge(self.tmp.name,self.embed,Vectors(),'trusted')
    def tearDown(self):self.tmp.cleanup()
    def load(self,items):return import_items(self.kb,validate({'items':items})[0],'test')
    def lexical(self,q):
        with patch.object(self.embed,'embed_text',side_effect=RuntimeError('offline')):return self.kb.query(q,'trusted')
    def test_lexical_short_long_identifiers_and_punctuation(self):
        self.load([item(text='退出 示例课程：点击“完成”！ SYNTH-001__ios')])
        for query in ['退出','示例课程','SYNTH-001','点击“完成”！']:
            with self.subTest(query=query):
                ev=self.lexical(query)['evidence'];self.assertTrue(ev);self.assertIn('exact',ev[0]['channels'])
    def test_backfill_repairs_either_index_and_triggers_update_delete(self):
        self.load([item(text='旧规则的唯一词')])
        with self.kb.db() as c:c.execute('DELETE FROM app_lex_substrings')
        self.assertEqual(backfill(self.kb)['processed'],1)
        new=self.load([item(text='新的导航唯一词')])['sources'][0]['version']
        ev=self.lexical('新的导航')['evidence'];self.assertEqual(ev[0]['source_version'],new)
        self.assertEqual(self.lexical('旧规则')['evidence'],[])
        with self.kb.db() as c:
            c.execute('UPDATE chunks SET text=? WHERE version=?',('更新后的唯一词',new))
        self.assertIn('exact',self.lexical('更新后的')['evidence'][0]['channels'])
        with self.kb.db() as c:
            c.execute('DELETE FROM chunks');self.assertEqual(c.execute('SELECT count(*) FROM app_lex_words').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM app_lex_substrings').fetchone()[0],0)
    def test_other_projects_cannot_crowd_candidates(self):
        self.load([item('target',project='one')]+[item(str(i),project='two') for i in range(40)])
        self.kb.filters={'project':'one','doc_type':[MAP_TYPE]}
        ev=self.lexical('示例功能')['evidence'];self.assertEqual(len(ev),1);self.assertEqual(ev[0]['metadata']['project'],'one')
        self.assertEqual(listing(self.kb,{'filters':{'doc_type':REPORT_TYPE}})['total'],0)
    def test_large_scope_uses_persistent_fts_and_keeps_exact(self):
        import uuid
        self.load([item(text='退出 示例课程：点击“完成”！ SYNTH-001__ios')])
        with self.kb.db() as c:
            row=c.execute('SELECT * FROM chunks LIMIT 1').fetchone()
            for i in range(501):
                c.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),row['source_id'],row['version'],'普通内容 '+str(i),'part',''))
        self.kb.filters={'project':'test_map'}
        for query in ['退出','示例课程','SYNTH-001','点击“完成”！']:
            ev=self.lexical(query)['evidence'];self.assertTrue(ev);self.assertIn('exact',ev[0]['channels'])
    def test_withdrawal_during_enrichment_removes_hit(self):
        self.load([item()]);original=self.kb.objects.get
        def withdraw(ref):
            value=original(ref)
            with self.kb.db() as c:c.execute("UPDATE publications SET state='withdrawn'")
            return value
        with patch.object(self.kb.objects,'get',side_effect=withdraw):self.assertEqual(self.lexical('示例功能')['evidence'],[])
    def test_busy_http_response_and_no_sensitive_log_message(self):
        from diagnostics import failure
        with patch('diagnostics.event'):
            self.assertNotIn('secret-data',failure(ValueError('secret-data'),'test')['message'])
        cfg={'KB_GATEWAY_TOKEN':'read','KB_COLLECTOR_PRINCIPAL':'trusted'}
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.kb,cfg))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            req=urllib.request.Request('http://127.0.0.1:%s/search'%server.server_port,
                data=json.dumps({'query':'test'}).encode(),headers={'Authorization':'Bearer read'})
            with patch('gateway.QUERY_SLOTS.acquire',return_value=False):
                with self.assertRaises(urllib.error.HTTPError) as result:urllib.request.urlopen(req)
                self.assertEqual(result.exception.code,503);self.assertEqual(json.load(result.exception)['error'],'query_busy')
        finally:server.shutdown();server.server_close();thread.join()
