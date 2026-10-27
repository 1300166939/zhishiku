"""Explicit publication registry for department-shared, read-only bearer credentials."""
import json,time
from pathlib import Path
from authorization import digest,validate_policy

def registry_path(policy_path):return Path(policy_path).with_name('shared-readers.json')
def load_registry(policy_path):
    path=registry_path(policy_path)
    if not path.exists():return {'version':1,'credentials':[]}
    if path.stat().st_size>1_000_000:raise ValueError('Shared registry capacity')
    data=json.loads(path.read_text())
    if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('credentials'),list):raise ValueError('Invalid registry')
    return data
def only_read(credential):
    grants=credential.get('grants',[])
    return bool(grants) and all(g.get('actions')==['read'] for g in grants)
def register(policy,registry,cid,token,label=None,default=False):
    credential=next(c for c in policy['credentials'] if c['credential_id']==cid)
    if not only_read(credential) or digest(token)!=credential['token_digest']:raise ValueError('Only matching read-only credentials can be shared')
    registry=json.loads(json.dumps(registry))
    if default:
        for row in registry['credentials']:row['default']=False
    registry['credentials']=[r for r in registry['credentials'] if r['credential_id']!=cid]
    registry['credentials'].append({'credential_id':cid,'token':token,'label':label or cid,'default':default})
    return registry
def export(policy,registry,now=None):
    validate_policy(policy);now=time.time() if now is None else now;out=[]
    for row in registry['credentials']:
        c=next((c for c in policy['credentials'] if c['credential_id']==row['credential_id']),None)
        active=bool(c and c['enabled'] and only_read(c) and digest(row['token'])==c['token_digest'] and (c.get('expires_at') is None or c['expires_at']>now))
        item={'credential_id':row['credential_id'],'label':row['label'],'default':row.get('default',False),'active':active,'grants':c['grants'] if active else []}
        if active:item['token']=row['token']
        out.append(item)
    if sum(bool(x['active'] and x['default']) for x in out)>1:raise ValueError('Ambiguous default reader')
    return {'version':1,'credentials':sorted(out,key=lambda x:x['credential_id'])}
