"""Local credential grants; no caller-owned role/project grants or remote ACL calls."""
import contextlib,fcntl,hashlib,hmac,json,math,re,time
from pathlib import Path
from diagnostics import event
DOMAINS={'common','app','test_api'}
ACTIONS={'read','create','update','import'}
ID=re.compile(r'[A-Za-z0-9_.:-]{1,128}')

class Forbidden(PermissionError):
    def __init__(self,code='permission_denied'):self.code=code;super().__init__(code)

def digest(token):return hashlib.sha256(token.encode()).hexdigest()

def validate_policy(policy):
    if not isinstance(policy,dict) or type(policy.get('version')) is not int or policy['version']<1:raise ValueError('Invalid policy version')
    creds=policy.get('credentials');ids=set();hashes=set()
    if not isinstance(creds,list) or len(creds)>500:raise ValueError('Invalid credentials')
    for c in creds:
        if not isinstance(c,dict) or not ID.fullmatch(c.get('credential_id','')) or c['credential_id'] in ids:raise ValueError('Invalid identity')
        if not re.fullmatch(r'[0-9a-f]{64}',c.get('token_digest','')) or c['token_digest'] in hashes:raise ValueError('Invalid digest')
        if type(c.get('enabled')) is not bool:raise ValueError('Invalid enabled flag')
        expiry=c.get('expires_at')
        if expiry is not None and (type(expiry) not in (int,float) or not math.isfinite(expiry)):raise ValueError('Invalid expiry')
        if not isinstance(c.get('grants'),list) or len(c['grants'])>50:raise ValueError('Invalid grants')
        for g in c['grants']:
            if not isinstance(g,dict) or g.get('domain') not in DOMAINS or not isinstance(g.get('actions'),list) or not set(g['actions'])<=ACTIONS:raise ValueError('Invalid grant')
            projects=g.get('projects')
            if projects!='*' and (not isinstance(projects,list) or not 1<=len(projects)<=500 or any(not isinstance(p,str) or not p or len(p)>256 or p=='*' for p in projects)):raise ValueError('Invalid projects')
            if g['domain']=='common' and (projects!='*' or set(g['actions'])-{'read'}):raise ValueError('Common writes remain trusted CLI only')
            default=g.get('default_project')
            if default is not None and (not isinstance(default,str) or not default or (projects!='*' and default not in projects)):raise ValueError('Invalid default project')
        ids.add(c['credential_id']);hashes.add(c['token_digest'])
    return policy

def compatibility(cfg):
    creds=[]
    for key,cid,write in [('KB_GATEWAY_TOKEN','legacy-team-reader',None),('KB_APP_WRITE_TOKEN','legacy-app-maintainer','app'),('KB_API_WRITE_TOKEN','legacy-api-maintainer','test_api')]:
        if not cfg.get(key):continue
        grants=[{'domain':d,'projects':'*','actions':['read']} for d in sorted(DOMAINS)]
        if write:grants.append({'domain':write,'projects':'*','actions':['create','update','import']})
        creds.append({'credential_id':cid,'token_digest':digest(cfg[key]),'enabled':True,'legacy_shared':True,'grants':grants})
    return {'version':1,'credentials':creds}

class Policy:
    def __init__(self,cfg):self.cfg=cfg;self.path=Path(cfg['KB_AUTH_POLICY']) if cfg.get('KB_AUTH_POLICY') else None
    def load(self):
        if not self.path:return compatibility(self.cfg)
        raw=self.path.read_bytes()
        if len(raw)>1_000_000:raise ValueError('Policy size limit')
        return validate_policy(json.loads(raw))
    @contextlib.contextmanager
    def locked(self):
        if not self.path:yield;return
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_SH)
            yield
    def credential(self,cid):
        p=self.load();now=time.time()
        c=next((c for c in p['credentials'] if c['credential_id']==cid and c['enabled'] and (c.get('expires_at') is None or c['expires_at']>now)),None)
        if c is None:raise Forbidden('credential_disabled')
        return c,p['version']
    def authenticate(self,header):
        if not isinstance(header,str) or not header.startswith('Bearer ') or len(header)>4096:return None
        target=digest(header[7:]);p=self.load();now=time.time()
        for c in p['credentials']:
            if hmac.compare_digest(target,c['token_digest']) and c['enabled'] and (c.get('expires_at') is None or c['expires_at']>now):return c['credential_id']
        return None
    def scope(self,cid,domain,action='read'):
        c,version=self.credential(cid)
        if domain not in DOMAINS or action not in ACTIONS:raise Forbidden()
        grants=[g for g in c['grants'] if g['domain']==domain and action in g['actions']]
        if not grants:raise Forbidden()
        if any(g['projects']=='*' for g in grants):projects=None
        else:projects=sorted({p for g in grants for p in g['projects']})
        return projects,version
    def allow(self,cid,domain,action,projects):
        permitted,version=self.scope(cid,domain,action)
        if permitted is not None and any(p is None or p not in permitted for p in projects):raise Forbidden('project_permission_denied')
        return version
    def read_scope(self,cid,domain,filters):
        projects,version=self.scope(cid,domain)
        selected=filters.get('project')
        if selected is not None:
            selected=selected if isinstance(selected,list) else [selected]
            if projects is not None and any(p not in projects for p in selected):raise Forbidden('project_permission_denied')
        return projects,version
    def default_project(self,cid,domain,action):
        c,_=self.credential(cid)
        values={g.get('default_project') for g in c['grants'] if g['domain']==domain and action in g['actions'] and g.get('default_project') is not None}
        if len(values)==1:return next(iter(values))
        projects,_=self.scope(cid,domain,action)
        if projects is not None and len(projects)==1:return projects[0]
        return None
    def context(self,cid,domain,action,projects):
        version=self.allow(cid,domain,action,projects)
        return {'credential_id':cid,'domain':domain,'action':action,'projects':sorted(set(projects),key=lambda x:x or ''),'policy_version':version}
    def check_context(self,ctx):
        return self.allow(ctx['credential_id'],ctx['domain'],ctx['action'],ctx['projects'])
    @contextlib.contextmanager
    def commit(self,contexts):
        with self.locked():
            for ctx in contexts:self.check_context(ctx)
            yield
    def public_identity(self,cid):
        c,v=self.credential(cid)
        return {'ok':True,'credential_id':cid,'owner':c.get('owner',cid),'legacy_shared':c.get('legacy_shared',False),'policy_version':v,'grants':c['grants'],'expires_at':c.get('expires_at')}

def audit(cid,action,domain,outcome,project=None,resource=None,request_id=None,reason=None):
    event('authorization',credential_id=cid,action=action,domain=domain,project=project,
          resource=resource,request_id=request_id,outcome=outcome,reason=reason)

def initialize_job_acl(c):
    c.execute('CREATE TABLE IF NOT EXISTS job_authorizations(job_id TEXT PRIMARY KEY,context TEXT NOT NULL)')

def bind_job(c,jid,context):
    initialize_job_acl(c)
    if context:c.execute('INSERT INTO job_authorizations VALUES(?,?)',(jid,json.dumps(context,sort_keys=True)))

def job_context(c,jid,cfg,kind):
    initialize_job_acl(c);r=c.execute('SELECT context FROM job_authorizations WHERE job_id=?',(jid,)).fetchone()
    if r:return json.loads(r[0])
    # Historical jobs have domain-wide legacy owner; project-only readers cannot see them.
    return {'credential_id':'legacy-app-maintainer','domain':'app','action':'import','projects':[None],'policy_version':0}

def guard_job_read(cfg,cid,jid,kind):
    import sqlite3
    with sqlite3.connect(Path(cfg['KB_APP_DATA_ROOT'])/'import-jobs.sqlite3') as c:ctx=job_context(c,jid,cfg,kind)
    Policy(cfg).allow(cid,'app','read',ctx['projects'])
    return ctx

def report_project(kb,batch):
    with kb.db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS report_ownership(batch_id TEXT PRIMARY KEY,project TEXT,credential_id TEXT)')
        r=c.execute('SELECT project FROM report_ownership WHERE batch_id=?',(batch,)).fetchone()
        exists=c.execute("SELECT 1 FROM app_reports WHERE batch_id=? AND status='published' LIMIT 1",(batch,)).fetchone()
    return (r[0] if r else None),bool(r or exists)

def report_context(cfg,cid,url,project=None):
    from gateway import knowledge
    from app_jobs import REPORT_URL
    policy=Policy(cfg);project=project if project is not None else policy.default_project(cid,'app','import')
    if project is not None and (not isinstance(project,str) or not project or len(project)>256):raise ValueError('Invalid report project')
    batch=REPORT_URL.fullmatch(url).group(1);kb=knowledge(cfg,'app');stored,exists=report_project(kb,batch)
    if exists:
        policy.allow(cid,'app','update',[stored])
        if stored!=project and project is not None:raise Forbidden('report_project_immutable')
        project=stored
    if not exists:policy.allow(cid,'app','create',[project])
    ctx=policy.context(cid,'app','import',[project]);ctx['batch_id']=batch
    return ctx

def map_contexts(policy,cid,kb,items):
    from app_maps import digest
    contexts=[policy.context(cid,'app','import',[i['metadata']['project'] for i in items])]
    for item in items:
        project=item['metadata']['project'];sid='app:map:'+digest([project,item['id']])
        with kb.db() as c:old=c.execute('SELECT 1 FROM sources WHERE source_id=? AND active_version IS NOT NULL',(sid,)).fetchone()
        contexts.append(policy.context(cid,'app','update' if old else 'create',[project]))
    return contexts

def api_contexts(policy,cid,kb,items):
    contexts=[]
    for item in items:
        meta=item['metadata'];project=meta.get('project');old=kb.item(item['id'])
        if old:
            stored=old['metadata'].get('project')
            policy.allow(cid,'test_api','update',[stored])
            if project!=stored:raise Forbidden('item_project_immutable')
        contexts.append(policy.context(cid,'test_api','update' if old else 'create',[project]))
    return contexts
