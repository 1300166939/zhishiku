import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from gateway import handler


class GatewayTests(unittest.TestCase):
    def setUp(self):
        class KB:
            def query(self,q,p,k):return {'principal':p,'query':q}
        self.server=ThreadingHTTPServer(('127.0.0.1',0),handler(KB(),{'KB_GATEWAY_TOKEN':'test-secret',
                                                        'KB_COLLECTOR_PRINCIPAL':'trusted'}))
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
    def tearDown(self):self.server.shutdown();self.thread.join();self.server.server_close()
    def request(self,path,body,token='test-secret'):
        return urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:'+str(self.server.server_port)+path,
            data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+token}),timeout=3)
    def test_identity_cannot_be_supplied(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/v1/agent-context/query',{'query':'q','principal':'other'})
        self.assertEqual(error.exception.code,400)
        with self.request('/v1/agent-context/query',{'query':'q'}) as response:
            self.assertEqual(json.load(response)['principal'],'trusted')
    def test_no_http_ingest_or_bad_token(self):
        for path,token,code in [('/v1/ingest','test-secret',404),('/v1/agent-context/query','wrong',401)]:
            with self.assertRaises(urllib.error.HTTPError) as error:self.request(path,{'query':'q'},token)
            self.assertEqual(error.exception.code,code)
    def test_qa_slot_rejects_second_request(self):
        started=threading.Event();release=threading.Event();result=[]
        body={'query':'q','filters':{'kb_domain':'common'}}
        def slow(*args):
            started.set();release.wait(3);return {'ok':True,'evidence':[]}
        def first():
            with self.request('/ask',body) as response:result.append(response.status)
        with patch('evidence_qa.query',side_effect=slow):
            worker=threading.Thread(target=first);worker.start()
            try:
                self.assertTrue(started.wait(1))
                with self.assertRaises(urllib.error.HTTPError) as error:self.request('/ask',body)
                self.assertEqual(error.exception.code,503)
                self.assertEqual(json.load(error.exception)['error'],'qa_busy')
            finally:release.set();worker.join()
        self.assertEqual(result,[200])


if __name__=='__main__':unittest.main()
