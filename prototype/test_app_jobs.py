import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from app_jobs import submit,status,validate_url,update,accept_package

URL='https://reports.example.test/files/reports/sample-batch-b/_summary.html'
class JobTests(unittest.TestCase):
    def test_no_arbitrary_source(self):
        self.assertEqual(validate_url(URL),URL)
        for bad in ['http://reports.example.test/files/reports/a/_summary.html',URL+'?x=1','https://127.0.0.1/_summary.html','https://reports.example.test@evil.test/files/reports/a/_summary.html','https://reports.example.test/files/reports/../_summary.html']:
            with self.assertRaises(ValueError):validate_url(bad)
    def test_job_request_binding_and_terminal_status(self):
        with tempfile.TemporaryDirectory() as root:
            cfg={'KB_APP_DATA_ROOT':root}
            with patch('app_jobs.subprocess.Popen'):
                first=submit(cfg,{'report_url':URL,'request_id':'test'})
                again=submit(cfg,{'report_url':URL,'request_id':'test'})
                self.assertEqual(first['job_id'],again['job_id'])
                with self.assertRaises(ValueError):submit(cfg,{'report_url':URL.replace('sample-batch-b','new'),'request_id':'test'})
            update(cfg,first['job_id'],'succeeded',{'idempotent':True})
            self.assertTrue(status(cfg,first['job_id'])['result']['idempotent'])
            with self.assertRaises(KeyError):status(cfg,'00000000-0000-0000-0000-000000000000')
    def test_package_path_and_metadata_limits(self):
        import zipfile,json
        with tempfile.TemporaryDirectory() as root:
            cfg={'KB_APP_DATA_ROOT':root};p=Path(root)/'package.zip'
            with zipfile.ZipFile(p,'w') as z:
                z.writestr('report.html','<html></html>');z.writestr('screenshots.json','[]')
                z.writestr('metadata.json',json.dumps({'report_url':URL,'request_id':'zip-test'}))
            with patch('app_jobs.subprocess.Popen'):
                job=accept_package(cfg,p);self.assertEqual(status(cfg,job['job_id'])['status'],'queued')
            bad=Path(root)/'bad.zip'
            with zipfile.ZipFile(bad,'w') as z:z.writestr('../escape','bad')
            with self.assertRaises(ValueError):accept_package(cfg,bad)
            self.assertFalse(bad.exists())

    def test_invalid_fields_and_queue_limit(self):
        with tempfile.TemporaryDirectory() as root:
            cfg={'KB_APP_DATA_ROOT':root}
            with self.assertRaises(ValueError):submit(cfg,{'report_url':URL,'library':'common'})
            with self.assertRaises(ValueError):submit(cfg,{'report_url':URL,'storage_mode':'full_images'})
            with patch('app_jobs.subprocess.Popen'):
                for i in range(4):submit(cfg,{'report_url':URL,'request_id':str(i)})
                with self.assertRaises(RuntimeError):submit(cfg,{'report_url':URL})

if __name__=='__main__':unittest.main()
