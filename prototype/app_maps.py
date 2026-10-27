"""Durable Token-submitted map text imports; no remote source/image fetching."""
from contextlib import nullcontext
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

from app_library import MAP_TYPE
from authorization import Policy,Forbidden,bind_job,job_context,map_contexts,audit

MAX_BODY=1_000_000
MAX_ITEMS=50
PROFILE='app-map-v1:paragraph-path:max1600:ark2048'
IDENTIFIER=re.compile(r'[A-Za-z0-9_.:-]{1,160}')
META={'kb_domain','doc_type','project','title','page_id','path','source_url','source_version','image_urls'}

class MapConflict(ValueError):
    pass

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

def validate(body):
    if not isinstance(body,dict) or set(body)-{'items','request_id'}:raise ValueError('Invalid map request fields')
    items=body.get('items');rid=body.get('request_id')
    if not isinstance(items,list) or not 1<=len(items)<=MAX_ITEMS:raise ValueError('Expected 1 to 50 items')
    if rid is not None and (not isinstance(rid,str) or not IDENTIFIER.fullmatch(rid)):raise ValueError('Invalid request_id')
    out=[];seen=set()
    for item in items:
        if not isinstance(item,dict) or set(item)-{'id','text','metadata','if_match'}:raise ValueError('Invalid map item fields')
        iid=item.get('id');text=item.get('text');meta=item.get('metadata',{})
        if not isinstance(iid,str) or not IDENTIFIER.fullmatch(iid):raise ValueError('Invalid map id')
        if not isinstance(text,str) or not text.strip() or len(text)>100_000:raise ValueError('Invalid map text')
        if not isinstance(meta,dict) or set(meta)-META:raise ValueError('Invalid map metadata fields')
        project=meta.get('project')
        if not isinstance(project,str) or not IDENTIFIER.fullmatch(project):raise ValueError('metadata.project required')
        if meta.get('kb_domain','app')!='app' or meta.get('doc_type',MAP_TYPE)!=MAP_TYPE:raise ValueError('Map endpoint accepts app_page_rule_chunk only')
        for key in META-{'image_urls'}:
            if key in meta and (not isinstance(meta[key],str) or len(meta[key])>2000):raise ValueError('Invalid metadata value')
        urls=meta.get('image_urls',[])
        if not isinstance(urls,list) or len(urls)>30:raise ValueError('Invalid image_urls')
        for url in [*urls,meta.get('source_url','')]:
            if url and (not isinstance(url,str) or len(url)>2048 or urlsplit(url).scheme not in {'http','https'} or not urlsplit(url).netloc or urlsplit(url).username):raise ValueError('Expected HTTP(S) source URLs')
        if any(not isinstance(u,str) or not u for u in urls):raise ValueError('Invalid image URL')
        if item.get('if_match') is not None and (not isinstance(item['if_match'],str) or len(item['if_match'])>160):raise ValueError('Invalid if_match')
        key=(project,iid)
        if key in seen:raise ValueError('Duplicate project/id in batch')
        seen.add(key)
        out.append({'id':iid,'text':text,'metadata':{**meta,'project':project,'kb_domain':'app','doc_type':MAP_TYPE},
                    **({'if_match':item['if_match']} if 'if_match' in item else {})})
    if len(json.dumps(out,ensure_ascii=False).encode())>MAX_BODY:raise ValueError('Map capacity limit')
    # Bound total model work per atomic batch, not just HTTP bytes.
    if sum(len(parts(i)) for i in out)>200:raise ValueError('At most 200 chunks per request; split into batches')
    return out,rid

def parts(item):
    meta=item['metadata'];prefix=meta.get('title',item['id'])+'\n'+meta.get('path','')
    prefix=prefix[:1200].strip()
    chunks=[];current='';heading=meta.get('title',item['id']);paragraph=''
    sections=[]
    for line in item['text'].splitlines():
        if line.startswith('#'):
            if paragraph.strip():sections.append((heading,paragraph));paragraph=''
            heading=line.lstrip('#').strip()
        elif not line.strip():
            if paragraph.strip():sections.append((heading,paragraph));paragraph=''
        else:paragraph+=line+'\n'
    if paragraph.strip():sections.append((heading,paragraph))
    for heading,paragraph in sections:
        # Keep short navigation chains together; only split long sections at line boundaries.
        for line in paragraph.splitlines():
            for start in range(0,len(line) or 1,1600):
                piece=line[start:start+1600]
                if current and len(current)+len(piece)>1600:
                    chunks.append({'text':prefix+'\n'+current.strip(),'heading_path':current_heading,'block_id':'part-'+str(len(chunks))});current=''
                if current and heading!=current_heading:
                    chunks.append({'text':prefix+'\n'+current.strip(),'heading_path':current_heading,'block_id':'part-'+str(len(chunks))});current=''
                if not current:current_heading=heading
                current+=piece+'\n'
    if current.strip():chunks.append({'text':prefix+'\n'+current.strip(),'heading_path':current_heading,'block_id':'part-'+str(len(chunks))})
    if not chunks:raise ValueError('No map text chunks')
    return chunks

def database(cfg):
    root=Path(cfg['KB_APP_DATA_ROOT']);root.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(root/'import-jobs.sqlite3',timeout=15);c.row_factory=sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('''CREATE TABLE IF NOT EXISTS map_jobs(job_id TEXT PRIMARY KEY,request_id TEXT UNIQUE,
                 input_hash TEXT,input_ref TEXT,status TEXT,result TEXT,created_at REAL,updated_at REAL)''')
    return c

def spawn(jid):
    subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--worker',jid],stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL,start_new_session=True)

def submit(cfg,body,auth_context=None):
    from storage import FileObjects
    items,rid=validate(body);data={'items':items};hash_=digest(data)
    with (Policy(cfg).commit([auth_context]) if auth_context else nullcontext()):
        c=database(cfg)
        try:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM map_jobs WHERE request_id=?',(rid,)).fetchone() if rid else None
            if old:
                if old['input_hash']!=hash_:raise MapConflict('request_id_reused_with_different_content')
                if auth_context:
                    prior=job_context(c,old['job_id'],cfg,'map')
                    if prior['projects']!=auth_context['projects']:raise Forbidden('request_scope_conflict')
                    Policy(cfg).allow(auth_context['credential_id'],'app','read',prior['projects'])
                return {'ok':True,'job_id':old['job_id'],'status':old['status'],'idempotent_request':True,'status_url':'/imports/'+old['job_id']}
            if c.execute("SELECT count(*) FROM map_jobs WHERE status IN ('queued','running')").fetchone()[0]>=4:raise MapConflict('map_queue_full')
            ref=FileObjects(Path(cfg['KB_APP_DATA_ROOT'])/'objects').put(json.dumps(data,ensure_ascii=False,sort_keys=True).encode())
            jid=str(uuid.uuid4());now=time.time()
            c.execute('INSERT INTO map_jobs VALUES(?,?,?,?,?,?,?,?)',(jid,rid,hash_,ref,'queued','{}',now,now));bind_job(c,jid,auth_context);c.commit()
        finally:c.close()
        try:spawn(jid)
        except Exception:
            update(cfg,jid,'failed',{'error':'worker_start_failed'});raise
    return {'ok':True,'job_id':jid,'status':'queued','status_url':'/imports/'+jid,'items':len(items),'doc_type':MAP_TYPE}

def update(cfg,jid,state,result):
    c=database(cfg)
    try:c.execute('UPDATE map_jobs SET status=?,result=?,updated_at=? WHERE job_id=?',(state,json.dumps(result,ensure_ascii=False),time.time(),jid));c.commit()
    finally:c.close()

def status(cfg,jid):
    try:uuid.UUID(jid)
    except (ValueError,TypeError):return None
    c=database(cfg)
    try:r=c.execute('SELECT * FROM map_jobs WHERE job_id=?',(jid,)).fetchone()
    finally:c.close()
    if not r:return None
    return {'ok':True,'job_id':jid,'status':r['status'],'kind':'app_map','result':json.loads(r['result']),
            'created_at':r['created_at'],'updated_at':r['updated_at']}

def import_items(kb,items,jid,auth_policy=None,auth_context=None):
    from import_app import CacheEmbedding
    if auth_context:map_contexts(auth_policy,auth_context['credential_id'],kb,items)
    kb.embedding=CacheEmbedding(kb,PROFILE)
    staged=[];unchanged=0
    for item in items:
        meta=item['metadata'];sid='app:map:'+digest([meta['project'],item['id']])
        parent={**meta,'text':item['text'],'title':meta.get('title',item['id'])}
        parent_ref=kb.objects.put(json.dumps(parent,ensure_ascii=False,sort_keys=True).encode())
        doc={'chunks':parts(item),'media':[],'parent_ref':parent_ref,'profile':PROFILE}
        hash_=digest(doc);version='map-v1-'+hash_[:24]
        with kb.db() as c:
            current=c.execute('SELECT active_version,tombstone FROM sources WHERE source_id=?',(sid,)).fetchone()
            baseline=current['active_version'] if current else None
            prior=c.execute('SELECT approved_by FROM publications WHERE source_id=? AND version=?',(sid,baseline)).fetchone() if baseline else None
        resumed_commit=baseline==version and prior and prior[0]=='gateway-map-token:'+jid
        if 'if_match' in item and item['if_match']!=(baseline or '') and not resumed_commit:raise MapConflict('if_match_failed')
        if current and current['tombstone']:raise MapConflict('source_withdrawn')
        if baseline==version:
            active=next((v for v in kb.authorized(kb.collector_principal) if v['source_id']==sid),None)
            if active:
                unchanged+=1;staged.append((sid,version,hash_,baseline,len(doc['chunks']),True));continue
        metadata={**meta,'item_id':item['id'],'title':parent['title'],'entity_kind':'map_page',
                  'storage_mode':'links_only','trust_status':'author_provided_map'}
        with kb.db() as c:c.execute('INSERT OR REPLACE INTO app_entities VALUES(?,?,?,?)',(sid,version,json.dumps(metadata,ensure_ascii=False),parent_ref))
        now=time.time()
        kb.ingest({'source_id':sid,'version':version,'title':parent['title'],'url':meta.get('source_url',''),
                   'document':doc,'proof':{'source_id':sid,'version':version,'principal':kb.collector_principal,
                   'view_allowed':True,'copy_allowed':True,'content_hash':hash_,'verified_at':now,'expires_at':now+900,
                   'authorization_ref':'gateway-map-token:'+jid}})
        staged.append((sid,version,hash_,baseline,len(doc['chunks']),False))
    # Reconcile the whole batch before changing any active source.
    for sid,version,hash_,baseline,count,same in staged:
        with kb.db() as c:
            row=c.execute('SELECT * FROM versions WHERE source_id=? AND version=?',(sid,version)).fetchone()
            chunks=c.execute('SELECT chunk_id FROM chunks WHERE source_id=? AND version=?',(sid,version)).fetchall()
            if row['content_hash']!=hash_ or len(chunks)!=count or row['status'] not in {'indexed','published'}:raise ValueError('Map reconciliation failed')
            if c.execute('SELECT count(*) FROM assets WHERE source_id=? AND version=?',(sid,version)).fetchone()[0]:raise ValueError('Map has image bytes')
        if hashlib.sha256(kb.objects.get(row['object_ref'])).hexdigest()!=hash_ or not all(kb.qdrant.exists(r[0]) for r in chunks):raise ValueError('Map vector reconciliation failed')
        item=next(i for i in items if 'app:map:'+digest([i['metadata']['project'],i['id']])==sid)
        if hasattr(kb.qdrant,'set_metadata'):
            kb.qdrant.set_metadata([r[0] for r in chunks],{'project':item['metadata']['project'],'doc_type':MAP_TYPE})
    contexts=map_contexts(auth_policy,auth_context['credential_id'],kb,items) if auth_context else []
    with (auth_policy.commit(contexts) if auth_context else nullcontext()),kb.db() as c:
        c.execute('BEGIN IMMEDIATE')
        for sid,version,hash_,baseline,count,same in staged:
            row=c.execute('SELECT active_version,tombstone FROM sources WHERE source_id=?',(sid,)).fetchone()
            if row['tombstone'] or row['active_version']!=baseline:raise MapConflict('source_changed_during_import')
            version_row=c.execute('SELECT content_hash,status FROM versions WHERE source_id=? AND version=?',(sid,version)).fetchone()
            if version_row['content_hash']!=hash_ or version_row['status'] not in {'indexed','published'}:raise MapConflict('staged_version_changed')
        for sid,version,hash_,baseline,count,same in staged:
            if same:continue
            approval='gateway-map-token:'+jid
            c.execute("INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,NULL) ON CONFLICT(source_id,version) DO UPDATE SET state='published',published_at=excluded.published_at,withdrawn_at=NULL",(sid,version,hash_,'app',approval,approval,time.time(),'published'))
            c.execute('UPDATE sources SET active_version=? WHERE source_id=?',(version,sid))
    return {'items':len(staged),'unchanged':unchanged,'published':len(staged)-unchanged,
            'model_calls':kb.embedding.model_calls,
            'chunks':sum(r[4] for r in staged),'image_downloads':0,'image_vectors':0,
            'sources':[{'id':items[i]['id'],'project':items[i]['metadata']['project'],'source_id':r[0],
                        'version':r[1],'chunks':r[4],'unchanged':r[5]} for i,r in enumerate(staged)]}

def worker(cfg,jid):
    from gateway import knowledge
    root=Path(cfg['KB_APP_DATA_ROOT'])
    with (root/'report-import.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        c=database(cfg)
        try:job=c.execute('SELECT * FROM map_jobs WHERE job_id=?',(jid,)).fetchone()
        finally:c.close()
        if not job or job['status']=='succeeded':return
        try:
            c=database(cfg)
            try:context=job_context(c,jid,cfg,'map') if cfg.get('KB_AUTH_POLICY') else None
            finally:c.close()
            if context:Policy(cfg).check_context(context)
            update(cfg,jid,'running',{'stage':'index_map_text'})
            kb=knowledge(cfg,'app');raw=kb.objects.get(job['input_ref']);data=json.loads(raw)
            if digest(data)!=job['input_hash']:raise ValueError('Map input hash mismatch')
            items,_=validate(data)
            result=import_items(kb,items,jid,auth_policy=Policy(cfg) if context else None,auth_context=context)
            if context:audit(context['credential_id'],'import','app','published',project=context['projects'],resource=str(len(items)),request_id=jid)
            update(cfg,jid,'succeeded',result)
        except Exception as exc:
            update(cfg,jid,'failed',{'error':str(exc) if isinstance(exc,MapConflict) else type(exc).__name__,
                   'previous_published_versions_preserved':True,'retry':'Resolve failure; submit a new request_id'})

def resume(cfg):
    c=database(cfg)
    try:ids=[r[0] for r in c.execute("SELECT job_id FROM map_jobs WHERE status IN ('queued','running')")]
    finally:c.close()
    for jid in ids:spawn(jid)

def listing(kb,body):
    if not isinstance(body,dict) or set(body)-{'filters','limit','offset'}:raise ValueError('Invalid map list fields')
    from app_library import validate_filters,matches
    filters=validate_filters(body.get('filters'));limit=body.get('limit',50);offset=body.get('offset',0)
    if type(limit) is not int or not 1<=limit<=100 or type(offset) is not int or offset<0:raise ValueError('Invalid pagination')
    if not matches({'doc_type':MAP_TYPE},{k:v for k,v in filters.items() if k=='doc_type'}):
        return {'ok':True,'doc_type':MAP_TYPE,'total':0,'results':[],'next_offset':None}
    from app_search import scope_sql
    scope,params=scope_sql(kb,filters={**filters,'doc_type':MAP_TYPE})
    with kb.db() as c:
        total=c.execute('SELECT count(*) FROM app_entities e'+scope,params).fetchone()[0]
        rows=c.execute('SELECT e.* FROM app_entities e'+scope+' ORDER BY e.source_id LIMIT ? OFFSET ?',params+[limit,offset]).fetchall()
    selected=[{'id':json.loads(r['metadata'])['item_id'],'version':r['version'],'source_id':r['source_id'],'metadata':json.loads(r['metadata'])} for r in rows]
    return {'ok':True,'doc_type':MAP_TYPE,'total':total,'results':selected,
            'next_offset':offset+limit if offset+limit<total else None}

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--worker',required=True);args=parser.parse_args()
    from gateway import settings
    worker(settings(),args.worker)
