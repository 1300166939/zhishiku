"""Links-only import: no source image fetching, atomic publication, exact URL evidence."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from app_library import AppKnowledge
from app_reports import parse_report
from app_jobs import worker,status,submit,accept_package
from import_app import records,import_report,report_version
from test_core import Embed,Vectors

DEP=Path(__file__).resolve().parents[1]/'deployment'
sys.path.insert(0,str(DEP))
import app_kb_client

class TextEmbed(Embed):
    model='doubao-embedding-vision-251215';dimensions=2048
    def embed_image(self,*a,**k):raise AssertionError('Image model must not be used')

class LinksTests(unittest.TestCase):
    def fixture(self):
        # Entirely synthetic report: no source screenshots or production logs.
        steps = ''.join(
            '<div class="tl-step"><span class="tl-step-idx">%s</span>'
            '<span class="tl-time">00:00:01</span>'
            '<div class="tl-shot"><h5>操作前</h5><img src="/files/before%s.png"></div>'
            '<div class="tl-shot"><h5>操作后</h5><img src="/files/after%s.png"></div></div>' % (i, i, i)
            for i in (1, 2))
        raw = ('<html><title>Synthetic report</title><h1>动作步骤测试</h1>'
               '<div class="hd-meta"><span class="chip"><b>Run ID</b> synthetic-run</span>'
               '<span class="chip"><b>用例 ID</b> SYNTH-1</span>'
               '<span class="chip"><b>步数</b> 2</span></div>'
               '<div class="runcontent">动作步骤：打开示例页面</div>'
               '<div class="timeline">' + steps + '</div></html>').encode()
        report = parse_report(raw, 'https://reports.example.test/files/reports/synthetic/_summary.html')
        report['storage_mode'] = 'links_only'
        return raw, report




    def test_client_fetches_html_only(self):
        url='https://reports.example.test/files/reports/sample/_summary.html'
        raw=b'<img src="https://reports.example.test/files/a.jpg">'
        with tempfile.TemporaryDirectory() as root,patch('app_kb_client.get',return_value=raw) as get:
            path=Path(root)/'package.zip';summary=app_kb_client.collect(url,path,'sample')
            self.assertEqual(get.call_count,1);self.assertEqual(summary['image_downloads'],0)
            with zipfile.ZipFile(path) as z:
                self.assertEqual(set(z.namelist()),{'report.html','metadata.json','screenshots.json'})
                self.assertEqual(json.loads(z.read('metadata.json'))['storage_mode'],'links_only')

    def test_client_project_preserved_without_image_downloads(self):
        url='https://reports.example.test/files/reports/sample/_summary.html'
        with tempfile.TemporaryDirectory() as root,patch('app_kb_client.get',return_value=b'<html/>') as get:
            path=Path(root)/'package.zip';app_kb_client.collect(url,path,'sample','new_app')
            with zipfile.ZipFile(path) as z:self.assertEqual(json.loads(z.read('metadata.json'))['project'],'new_app')
            self.assertEqual(get.call_count,1)

    def test_explicit_links_package_rejects_image_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'package.zip'
            with zipfile.ZipFile(path,'w') as z:
                z.writestr('report.html','<html/>');z.writestr('screenshots.json','[]')
                z.writestr('metadata.json',json.dumps({'report_url':'https://reports.example.test/files/reports/test/_summary.html','storage_mode':'links_only'}))
                z.writestr('images/'+'a'*64,b'image')
            with self.assertRaises(ValueError):
                accept_package({'KB_APP_DATA_ROOT':root},path)
            self.assertFalse(path.exists())


if __name__=='__main__':unittest.main()
