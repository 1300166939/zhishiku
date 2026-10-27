#!/usr/bin/env python3
"""Collect an App HTML report inside company network, then import it using a write Token."""
import argparse
import concurrent.futures
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.request
from urllib.parse import urljoin,urlparse
import zipfile

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k):return None

class Images(HTMLParser):
    def __init__(self):super().__init__();self.urls=[]
    def handle_starttag(self,tag,attrs):
        if tag=='img':
            src=dict(attrs).get('src')
            if src:self.urls.append(src)

def get(url,limit):
    with urllib.request.build_opener(NoRedirect()).open(url,timeout=40) as r:data=r.read(limit+1)
    if not data or len(data)>limit:raise ValueError('Resource size limit')
    return data

def collect(url,target,request_id,project=None):
    if not re.fullmatch(r'https://reports\.example\.test/files/reports/[A-Za-z0-9_-]{1,80}/[A-Za-z0-9_-]{1,160}\.html',url):
        raise ValueError('Unsupported report URL')
    raw=get(url,20_000_000);parser=Images();parser.feed(raw.decode('utf-8'))
    urls=sorted({urljoin(url,s) for s in parser.urls})
    if len(urls)>5000:raise ValueError('Screenshot capacity limit')
    # Only HTML is collected. Screenshot URLs are validated and retained without fetching bytes.
    for u in urls:
        p=urlparse(u)
        if p.scheme!='https' or p.netloc!='reports.example.test' or not p.path.startswith('/files/') or p.query or p.fragment:raise ValueError('Screenshot origin')
    metadata={'report_url':url,'request_id':request_id,'storage_mode':'links_only'}
    if project is not None:
        if not isinstance(project,str) or not project or len(project)>256:raise ValueError('Invalid project')
        metadata['project']=project
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=3) as z:
        z.writestr('report.html',raw)
        z.writestr('screenshots.json',json.dumps(urls))
        z.writestr('metadata.json',json.dumps(metadata))
    return {'storage_mode':'links_only','screenshot_references':len(urls),'image_downloads':0,
            'html_bytes':len(raw),'package_bytes':Path(target).stat().st_size}

def upload(package,base,token):
    import http.client
    p=urlparse(base)
    if p.scheme not in {'http','https'} or not p.hostname or p.username or p.password or p.query or p.fragment:raise ValueError('Invalid gateway URL')
    cls=http.client.HTTPSConnection if p.scheme=='https' else http.client.HTTPConnection
    conn=cls(p.hostname,p.port,timeout=180)
    try:
        conn.putrequest('POST',p.path.rstrip('/')+'/imports/app/packages')
        conn.putheader('Authorization','Bearer '+token);conn.putheader('Content-Type','application/zip')
        conn.putheader('Content-Length',str(Path(package).stat().st_size));conn.endheaders()
        with open(package,'rb') as f:
            while True:
                data=f.read(1024*1024)
                if not data:break
                conn.send(data)
        response=conn.getresponse();value=json.loads(response.read())
        if response.status!=202:raise RuntimeError('Gateway HTTP '+str(response.status)+': '+value.get('error','unknown'))
        return value
    finally:conn.close()

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report-url',required=True);p.add_argument('--request-id')
    p.add_argument('--project',help='Dedicated library project; project-scoped Token can also supply its default')
    p.add_argument('--base-url',default=os.environ.get('KB_BASE_URL'));p.add_argument('--package',type=Path);p.add_argument('--collect-only',action='store_true')
    a=p.parse_args();rid=a.request_id or 'app-'+str(int(time.time()));target=a.package or Path('app-report-package.zip')
    summary=collect(a.report_url,target,rid,a.project);print(json.dumps(summary))
    if a.collect_only:return
    token=os.environ.get('KB_WRITE_TOKEN') or os.environ.get('KB_TOKEN')
    if not a.base_url or not token:raise ValueError('Set KB_BASE_URL and a write-capable KB_TOKEN or KB_WRITE_TOKEN')
    result=upload(target,a.base_url,token);print(json.dumps(result))
    print('Poll GET '+a.base_url.rstrip('/')+result['status_url']+' using your Token')

if __name__=='__main__':
    try:main()
    except Exception as e:print('Import failed: '+type(e).__name__+' '+str(e));raise SystemExit(1)
