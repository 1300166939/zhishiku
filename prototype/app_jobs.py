"""Token-authorized, asynchronous imports for the approved App report producer."""
import fcntl
from contextlib import nullcontext
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile
import os
from pathlib import Path
from urllib.parse import urlparse
from diagnostics import failure,event
from authorization import Policy,Forbidden,bind_job,job_context,report_context,audit

REPORT_URL=re.compile(r'https://reports\.example\.test/files/reports/([A-Za-z0-9_-]{1,80})/[A-Za-z0-9_-]{1,160}\.html')
MAX_HTML=20_000_000;MAX_IMAGE=10_000_000;MAX_URLS=5000;MAX_BYTES=2_000_000_000
MAX_PACKAGE_BYTES=1_500_000_000

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):return None

def validate_url(url):
    if not isinstance(url,str) or not REPORT_URL.fullmatch(url):raise ValueError('Unsupported report URL')
    return url

def fetch(url,limit):
    with urllib.request.build_opener(NoRedirect()).open(url,timeout=40) as response:
        data=response.read(limit+1)
    if not data or len(data)>limit:raise ValueError('Resource size limit')
    return data

def jobs_db(cfg):
    path=Path(cfg['KB_APP_DATA_ROOT']);path.mkdir(exist_ok=True,parents=True)
    c=sqlite3.connect(path/'import-jobs.sqlite3',timeout=15);c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('''CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY, report_url TEXT, request_id TEXT UNIQUE,
             status TEXT, result TEXT, created_at REAL, updated_at REAL)''')
    return c

def submit(cfg,body,package_path=None,auth_context=None):
    if not isinstance(body,dict) or set(body)-{'report_url','request_id','storage_mode','project'}:raise ValueError('Invalid import fields')
    mode=body.get('storage_mode','links_only')
    if mode!='links_only':raise ValueError('App supports links_only storage mode only')
    url=validate_url(body.get('report_url'));rid=body.get('request_id')
    if rid is not None and (not isinstance(rid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',rid)):
        raise ValueError('Invalid request_id')
    with (Policy(cfg).commit([auth_context]) if auth_context else nullcontext()):
        c=jobs_db(cfg)
        try:
            c.execute('BEGIN IMMEDIATE')
            if rid:
                old=c.execute('SELECT * FROM jobs WHERE request_id=?',(rid,)).fetchone()
                if old:
                    if old['report_url']!=url:raise ValueError('request_id reused for different report')
                    if json.loads(old['result']).get('storage_mode','full_images')!=mode:raise ValueError('request_id reused for different mode')
                    if auth_context:
                        prior=job_context(c,old['job_id'],cfg,'report')
                        if prior['projects']!=auth_context['projects']:raise Forbidden('request_scope_conflict')
                        Policy(cfg).allow(auth_context['credential_id'],'app','read',prior['projects'])
                    c.commit();return {'job_id':old['job_id'],'status':old['status'],'idempotent_request':True}
            active=c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if active>=4:raise RuntimeError('import_queue_full')
            jid=str(uuid.uuid4());now=time.time()
            result={'storage_mode':mode}
            if package_path:result['package_path']=str(package_path)
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',(jid,url,rid,'queued',json.dumps(result),now,now));bind_job(c,jid,auth_context);c.commit()
        finally:c.close()
        try:
            subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker',jid],
                             stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,start_new_session=True)
        except Exception:
            update(cfg,jid,'failed',{'error':'worker_start_failed'});raise
    return {'job_id':jid,'status':'queued','status_url':'/v1/app/import-jobs/'+jid}

def accept_package(cfg,path,credential_id=None):
    path=Path(path)
    try:
        with zipfile.ZipFile(path) as z:
            members=z.infolist()
            if len(members)>MAX_URLS+3 or sum(x.file_size for x in members)>MAX_BYTES+MAX_HTML+5_000_000:
                raise ValueError('Package capacity limit')
            if len({m.filename for m in members})!=len(members):raise ValueError('Duplicate package members')
            for m in members:
                if m.filename not in {'report.html','metadata.json','screenshots.json'}:
                    raise ValueError('Invalid package entry')
            if z.getinfo('report.html').file_size>MAX_HTML or z.getinfo('metadata.json').file_size>16384 or any(z.getinfo(n).file_size>5_000_000 for n in ['downloads.json','screenshots.json'] if n in z.namelist()):
                raise ValueError('Package metadata limit')
            meta=json.loads(z.read('metadata.json'))
            validate_url(meta.get('report_url'))
            if set(z.namelist())!={'report.html','metadata.json','screenshots.json'}:
                raise ValueError('Links-only package must contain HTML and URL manifest only')
        context=report_context(cfg,credential_id,meta['report_url'],meta.get('project')) if credential_id else None
        job=submit(cfg,meta,package_path=path,auth_context=context)
        if job.get('idempotent_request'):path.unlink(missing_ok=True)
        return job
    except Exception:
        path.unlink(missing_ok=True);raise

def status(cfg,jid):
    try:uuid.UUID(jid)
    except (ValueError,TypeError):raise ValueError('Invalid job_id')
    c=jobs_db(cfg)
    try:r=c.execute('SELECT * FROM jobs WHERE job_id=?',(jid,)).fetchone()
    finally:c.close()
    if not r:raise KeyError('No job')
    result=json.loads(r['result']);result.pop('package_path',None)
    return {'job_id':jid,'status':r['status'],'result':result,'created_at':r['created_at'],'updated_at':r['updated_at']}

def resume_pending(cfg):
    c=jobs_db(cfg)
    try:ids=[r[0] for r in c.execute("SELECT job_id FROM jobs WHERE status IN ('queued','running')")]
    finally:c.close()
    for jid in ids:
        subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker',jid],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,start_new_session=True)

def update(cfg,jid,state,result):
    c=jobs_db(cfg)
    try:
        old=c.execute('SELECT result FROM jobs WHERE job_id=?',(jid,)).fetchone()
        if old:
            previous=json.loads(old[0]);package=previous.get('package_path')
            result={**result,'storage_mode':previous.get('storage_mode','full_images')}
            if package:result={**result,'package_path':package}
        c.execute('UPDATE jobs SET status=?,result=?,updated_at=? WHERE job_id=?',(state,json.dumps(result),time.time(),jid));c.commit()
    finally:c.close()

def worker(cfg,jid):
    from gateway import knowledge
    from app_reports import parse_report
    from import_app import import_report,report_version
    root=Path(cfg['KB_APP_DATA_ROOT'])
    # Serializes all HTTP report jobs; imports hold their own durable checkpoint.
    with (root/'report-import.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        stage='load_job'
        try:
            c=jobs_db(cfg)
            try:job=dict(c.execute('SELECT * FROM jobs WHERE job_id=?',(jid,)).fetchone())
            finally:c.close()
            if job['status'] in {'succeeded','cancelled'}:return
            c=jobs_db(cfg)
            try:context=job_context(c,jid,cfg,'report') if cfg.get('KB_AUTH_POLICY') else None
            finally:c.close()
            if context:Policy(cfg).check_context(context)
            previous=json.loads(job['result']);mode=previous.get('storage_mode','links_only')
            if mode!='links_only':raise ValueError('Legacy image import disabled')
            package=previous.get('package_path');url=job['report_url'];validate_url(url)
            stage='parse_package' if package else 'fetch_report'
            event('report_stage',job_id=jid,request_id=job['request_id'],stage=stage)
            update(cfg,jid,'running',{'stage':stage,'package_path':package} if package else {'stage':stage})
            if package:
                z=zipfile.ZipFile(package);raw=z.read('report.html')
                if len(raw)>MAX_HTML:raise ValueError('Report size limit')
            else:raw=fetch(url,MAX_HTML)
            stage='parse_html';report=parse_report(raw,url)
            if not report['runs'] or report['counts']['steps']>2500 or report['counts']['screenshot_urls']>MAX_URLS:
                raise ValueError('Report capacity limit')
            kb=knowledge(cfg,'app');version=report_version(report)
            if context:
                fresh=report_context(cfg,context['credential_id'],url,context['projects'][0])
                if fresh['projects']!=context['projects']:raise Forbidden('report_project_changed')
            if mode=='links_only':
                stage='validate_screenshot_links'
                required={s['url'] for r in report['runs'] for step in r['steps'] for s in step['screenshots']}
                for u in required:
                    parsed=urlparse(u)
                    if parsed.scheme!='https' or parsed.netloc!='reports.example.test' or not parsed.path.startswith('/files/') or parsed.query or parsed.fragment:raise ValueError('Invalid screenshot URL')
                if package:
                    inventory=json.loads(z.read('screenshots.json'))
                    if not isinstance(inventory,list) or len(inventory)!=len(set(inventory)) or set(inventory)!=required:raise ValueError('Incomplete screenshot links')
                    z.close()
                report['storage_mode']='links_only';version=report_version(report)
                stage='index_text';event('report_stage',job_id=jid,stage=stage)
                update(cfg,jid,'running',{'stage':stage,'image_downloads':0})
                output=import_report(kb,report,{},raw,checkpoint=root/('job-'+jid+'.json'),workers=4,authorization_ref='gateway-write-token:job:'+jid,auth_policy=Policy(cfg) if context else None,auth_context=context)
                if context:audit(context['credential_id'],'import','app','published',project=context['projects'][0],resource=report['batch_id'],request_id=jid)
                result={'batch_id':report['batch_id'],'idempotent':output.get('idempotent',False),'model_calls':output.get('model_calls',0),
                        'published_records':output['done']-output['failures'],'failures':output['failures'],
                        'coverage':output.get('coverage',report['counts']),'input_coverage':output.get('input_coverage',report['counts']),
                        'coverage_mismatches':output.get('coverage_mismatches',report['coverage_mismatches'])}
                result.update(image_downloads=0,image_vectors=0,image_coverage='links_only_not_indexed')
                update(cfg,jid,'succeeded' if not result['failures'] else 'partial',result)
                if package and not result['failures']:
                    Path(package).unlink(missing_ok=True)
                    c=jobs_db(cfg)
                    try:
                        value=json.loads(c.execute('SELECT result FROM jobs WHERE job_id=?',(jid,)).fetchone()[0]);value.pop('package_path',None);value['package_cleaned']=True
                        c.execute('UPDATE jobs SET result=? WHERE job_id=?',(json.dumps(value),jid));c.commit()
                    finally:c.close()
                return
        except Exception as exc:
            details=failure(exc,stage,job_id=jid)
            update(cfg,jid,'failed',{**details,'retry':'Submit a new request_id after resolving source/network failure'})

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--worker',required=True);args=parser.parse_args()
    from gateway import settings
    worker(settings(),args.worker)
