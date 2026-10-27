"""Explicit, local-admin governance. Never infer business validity from filenames."""
import json
import time

FIELDS = {'family', 'module', 'doc_type', 'state', 'valid_from', 'valid_until',
          'supersedes', 'authority_ref', 'knowledge_kind'}

def initialize(c):
    c.execute('''CREATE TABLE IF NOT EXISTS knowledge_governance(
      source_id TEXT,version TEXT,metadata TEXT NOT NULL,updated_at REAL NOT NULL,
      PRIMARY KEY(source_id,version), FOREIGN KEY(source_id,version) REFERENCES versions(source_id,version))''')
    c.execute('''CREATE TABLE IF NOT EXISTS governance_audit(
      id INTEGER PRIMARY KEY,source_id TEXT,version TEXT,actor TEXT,reason TEXT,
      before_json TEXT,after_json TEXT,created_at REAL)''')

def register(kb, source, version, metadata, actor, reason):
    if not actor or not reason or not isinstance(metadata, dict) or set(metadata)-FIELDS:
        raise ValueError('Invalid governance registration')
    m = dict(metadata)
    if m.get('state','unknown') not in {'unknown','draft','approved','retired'}:
        raise ValueError('Invalid knowledge state')
    if m.get('knowledge_kind','original') not in {'original','derived','business_verified'}:
        raise ValueError('Invalid knowledge kind')
    for key in ['family','module','doc_type','authority_ref']:
        if key in m and (not isinstance(m[key],str) or not m[key] or len(m[key])>500):
            raise ValueError('Invalid governance string')
    for key in ['valid_from','valid_until']:
        if key in m and (type(m[key]) not in {int,float} or m[key]<0):
            raise ValueError('Invalid validity time')
    if m.get('valid_from',0)>=m.get('valid_until',float('inf')):
        raise ValueError('Invalid validity interval')
    if (m.get('state')=='approved' or m.get('knowledge_kind')=='business_verified') and not m.get('authority_ref'):
        raise ValueError('Confirmed knowledge needs authority evidence')
    links=m.get('supersedes',[])
    if not isinstance(links,list) or len(links)>50 or any(not isinstance(x,dict) or set(x)!={'source_id','version'} or any(not isinstance(v,str) for v in x.values()) for x in links):
        raise ValueError('Invalid supersedes references')
    with kb.db() as c:
        initialize(c)
        if not c.execute('SELECT 1 FROM versions WHERE source_id=? AND version=?',(source,version)).fetchone():
            raise ValueError('Unknown source version')
        for link in links:
            if (link['source_id'],link['version'])==(source,version) or not c.execute('SELECT 1 FROM versions WHERE source_id=? AND version=?',(link['source_id'],link['version'])).fetchone():
                raise ValueError('Invalid superseded source')
        prior=c.execute('SELECT metadata FROM knowledge_governance WHERE source_id=? AND version=?',(source,version)).fetchone()
        encoded=json.dumps(m,sort_keys=True,ensure_ascii=False)
        if prior and prior[0]==encoded:return {'changed':False}
        # Cycles would invalidate both sides; reject them before publishing governance.
        graph={ (r[0],r[1]):[(x['source_id'],x['version']) for x in json.loads(r[2]).get('supersedes',[])] for r in c.execute('SELECT source_id,version,metadata FROM knowledge_governance')}
        graph[(source,version)]=[(x['source_id'],x['version']) for x in links]
        def visit(key,path):
            if key in path:raise ValueError('Supersedes cycle')
            for child in graph.get(key,[]):visit(child,path|{key})
        visit((source,version),set())
        c.execute('INSERT OR REPLACE INTO knowledge_governance VALUES(?,?,?,?)',(source,version,encoded,time.time()))
        c.execute('INSERT INTO governance_audit VALUES(NULL,?,?,?,?,?,?,?)',(source,version,actor,reason,prior[0] if prior else None,encoded,time.time()))
    return {'changed':True}

def resolve(kb, authorized, at=None):
    """Only authorized, valid successors may suppress an older source."""
    at=time.time() if at is None else at
    with kb.db() as c:
        initialize(c)
        meta={(r[0],r[1]):json.loads(r[2]) for r in c.execute('SELECT source_id,version,metadata FROM knowledge_governance')}
    valid={}
    for r in authorized:
        key=(r['source_id'],r['version']);m=meta.get(key,{'state':'unknown','knowledge_kind':'original'})
        if m.get('state')=='retired' or not m.get('valid_from',0)<=at<m.get('valid_until',float('inf')):continue
        valid[key]=m
    superseded=set()
    for m in valid.values():
        if m.get('state')=='approved' and m.get('authority_ref'):
            superseded.update((x['source_id'],x['version']) for x in m.get('supersedes',[]))
    return {k:m for k,m in valid.items() if k not in superseded}
