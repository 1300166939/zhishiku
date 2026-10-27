"""Manual local-admin connector outbox. No scheduler, arbitrary URL fetch or scripts."""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

def validate(envelope):
    if not isinstance(envelope,dict) or set(envelope)!={'schema_version','connector','source_id','version','request_id','payload'}:
        raise ValueError('Invalid connector envelope')
    if envelope['schema_version']!=1 or envelope['connector'] not in {'common_bundle','api_items','app_maps'}:
        raise ValueError('Unsupported connector')
    for k in ['source_id','version','request_id']:
        if not isinstance(envelope[k],str) or not 1<=len(envelope[k])<=200:raise ValueError('Invalid connector identity')
    if not isinstance(envelope['payload'],dict):raise ValueError('Invalid payload')
    raw=json.dumps(envelope,ensure_ascii=False,sort_keys=True).encode()
    if len(raw)>2_000_000:raise ValueError('Connector payload limit')
    # Tokens are provided by operator configuration, never persisted in an envelope.
    def secrets(value):
        if isinstance(value,dict):
            for k,v in value.items():
                if any(x in k.lower() for x in ['password','secret','token','authorization','api_key']):raise ValueError('Secret field in envelope')
                secrets(v)
        elif isinstance(value,list):
            for v in value:secrets(v)
    secrets(envelope)
    if envelope['connector']=='common_bundle':
        b=envelope['payload'].get('bundle',{})
        if b.get('source_id')!=envelope['source_id'] or str(b.get('version'))!=envelope['version']:
            raise ValueError('Bundle identity mismatch')
        publication=envelope['payload'].get('publication')
        if publication is not None and (publication.get('source_id')!=envelope['source_id'] or str(publication.get('version'))!=envelope['version']):
            raise ValueError('Publication identity mismatch')
    elif envelope['connector']=='api_items':
        from api_library import validate_items
        validate_items(envelope['payload'])
    else:
        from app_maps import validate
        validate(envelope['payload'])
    return raw

class Queue:
    def __init__(self,root,capacity=100):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.path=self.root/'connectors.sqlite3';self.capacity=capacity
        with self.db() as c:c.execute('''CREATE TABLE IF NOT EXISTS connector_jobs(
          request_id TEXT PRIMARY KEY,digest TEXT,payload TEXT,status TEXT,attempts INTEGER,
          lease_id TEXT,lease_until REAL,retry_at REAL,result TEXT,error TEXT)''')
        with self.db() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS connector_events(
              id INTEGER PRIMARY KEY,request_id TEXT,state TEXT,error TEXT,created_at REAL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS connector_checkpoints(
              connector TEXT,source_id TEXT,version TEXT,request_id TEXT,digest TEXT,updated_at REAL,
              PRIMARY KEY(connector,source_id))''')
        self.path.chmod(0o600)
    @contextmanager
    def db(self):
        c=sqlite3.connect(self.path,timeout=10);c.row_factory=sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback();raise
        finally:c.close()
    def submit(self,envelope):
        raw=validate(envelope);digest=hashlib.sha256(raw).hexdigest();rid=envelope['request_id']
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT digest,status,result FROM connector_jobs WHERE request_id=?',(rid,)).fetchone()
            if prior:
                if prior['digest']!=digest:raise ValueError('Idempotency conflict')
                return {'request_id':rid,'status':prior['status'],'idempotent':True}
            if c.execute("SELECT count(*) FROM connector_jobs WHERE status IN ('queued','running','retry')").fetchone()[0]>=self.capacity:
                raise ValueError('Queue capacity reached')
            c.execute('INSERT INTO connector_jobs VALUES(?,?,?,\'queued\',0,NULL,0,0,NULL,NULL)',(rid,digest,raw.decode()))
            c.execute('INSERT INTO connector_events VALUES(NULL,?,?,NULL,?)',(rid,'queued',time.time()))
        return {'request_id':rid,'status':'queued','idempotent':False}
    def run_one(self,adapters,now=None,lease_seconds=300):
        real_clock=now is None
        now=time.time() if now is None else now;lease=str(uuid.uuid4())
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE connector_jobs SET status='retry',lease_id=NULL,retry_at=? WHERE status='running' AND lease_until<=?",(now,now))
            r=c.execute("SELECT * FROM connector_jobs WHERE status IN ('queued','retry') AND retry_at<=? ORDER BY rowid LIMIT 1",(now,)).fetchone()
            if not r:return {'status':'idle'}
            c.execute("UPDATE connector_jobs SET status='running',attempts=attempts+1,lease_id=?,lease_until=? WHERE request_id=?",(lease,now+lease_seconds,r['request_id']))
            c.execute('INSERT INTO connector_events VALUES(NULL,?,?,NULL,?)',(r['request_id'],'running',now))
        e=json.loads(r['payload']);result=None;error=None
        try:
            if e['connector'] not in adapters:raise ValueError('Missing trusted adapter')
            result=adapters[e['connector']](e)
        except Exception as exc:error=type(exc).__name__
        with self.db() as c:
            status='done' if error is None else ('failed' if r['attempts']>=4 else 'retry')
            changed=c.execute('''UPDATE connector_jobs SET status=?,result=?,error=?,retry_at=?,lease_id=NULL
              WHERE request_id=? AND lease_id=? AND lease_until>?''',
              (status,json.dumps(result,ensure_ascii=False) if result is not None else None,error,now+2**(r['attempts']+1),r['request_id'],lease,time.time() if real_clock else now)).rowcount
            if not changed:return {'status':'lease_lost','request_id':r['request_id']}
            c.execute('INSERT INTO connector_events VALUES(NULL,?,?,?,?)',(r['request_id'],status,error,now))
            if status=='done':c.execute('INSERT OR REPLACE INTO connector_checkpoints VALUES(?,?,?,?,?,?)',
                (e['connector'],e['source_id'],e['version'],r['request_id'],r['digest'],now))
        return {'status':status,'request_id':r['request_id'],'error':error}

def adapters(cfg):
    from gateway import knowledge
    def common(e):
        kb=knowledge(cfg);payload=e['payload'];b=payload['bundle']
        # Fresh proof and explicit local approval are still mandatory on retries.
        result=kb.ingest(b);kb.publish(payload['publication']);return result
    def api(e):
        import fcntl
        from api_library import validate_items
        kb=knowledge(cfg,'test_api')
        with (kb.root/'import.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return kb.upsert(validate_items(e['payload']),approval='connector-local-admin',approval_ref=e['request_id'])
    def app(e):
        import fcntl
        from app_maps import validate,import_items
        kb=knowledge(cfg,'app');items,_=validate(e['payload'])
        # Complete inside the capped worker; do not detach children from a oneshot.
        with (kb.root/'report-import.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return import_items(kb,items,e['request_id'])
    return {'common_bundle':common,'api_items':api,'app_maps':app}

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('root');p.add_argument('--submit');p.add_argument('--run-one',action='store_true');a=p.parse_args()
    q=Queue(a.root)
    if a.submit:r=q.submit(json.loads(Path(a.submit).read_text()))
    elif a.run_one:
        from gateway import settings
        r=q.run_one(adapters(settings()))
    else:r={'status':'manual_only','connectors':['common_bundle','api_items','app_maps'],'web':'unsupported'}
    print(json.dumps(r,ensure_ascii=False))
