"""Operator-only cloud token retention. Never used by public HTTP discovery/publisher."""
import argparse,fcntl,json,time
from pathlib import Path
from authorization import digest,validate_policy,ID

def vault_path(policy_path):return Path(policy_path).with_name('credential-vault.json')
def load_vault(policy_path):
    path=vault_path(policy_path)
    if not path.exists():return {'version':1,'credentials':[]}
    if path.stat().st_mode&0o077:raise ValueError('Token vault must be private')
    if path.stat().st_size>1_000_000:raise ValueError('Token vault capacity')
    data=json.loads(path.read_text())
    if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('credentials'),list):raise ValueError('Invalid token vault')
    seen=set()
    for row in data['credentials']:
        if not isinstance(row,dict) or not ID.fullmatch(row.get('credential_id','')) or row['credential_id'] in seen:raise ValueError('Invalid vault identity')
        if not isinstance(row.get('token'),str) or not row['token']:raise ValueError('Invalid saved token')
        seen.add(row['credential_id'])
    return data

def retain(policy,vault,cid,token):
    cred=next(c for c in policy['credentials'] if c['credential_id']==cid)
    if digest(token)!=cred['token_digest']:raise ValueError('Saved token does not match policy')
    vault=json.loads(json.dumps(vault))
    vault['credentials']=[r for r in vault['credentials'] if r['credential_id']!=cid]
    vault['credentials'].append({'credential_id':cid,'token':token,'saved_at':time.time()})
    return vault

def libraries(policy,vault,catalog,library_id=None,show_tokens=False):
    validate_policy(policy);result=[]
    for row in catalog['libraries']:
        if library_id is not None and row['library_id']!=library_id:continue
        item={k:row.get(k) for k in ['library_id','name','owner','kb_domain','project','enabled','rerank_enabled']}
        for role in ['read','write']:
            cid=row.get(role+'_credential_id');cred=next((c for c in policy['credentials'] if c['credential_id']==cid),None)
            saved=next((c for c in vault['credentials'] if c['credential_id']==cid),None)
            matches=bool(cred and saved and digest(saved['token'])==cred['token_digest'])
            active=bool(cred and cred['enabled'] and (cred.get('expires_at') is None or cred['expires_at']>time.time()))
            value={'credential_id':cid,'saved':matches,'active':active,'status':'saved' if matches else ('stale' if saved else 'not_saved')}
            if show_tokens and matches:value['token']=saved['token']
            item[role]=value
        result.append(item)
    if library_id is not None and not result:raise ValueError('Library not found')
    return {'ok':True,'libraries':result}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--policy',default='/var/lib/ai-knowledge-base/access/policy.json');p.add_argument('--routes',default='/var/lib/ai-knowledge-base/access/library-routes.json')
    p.add_argument('--library-id');p.add_argument('--show-tokens',action='store_true',help='Explicitly display saved raw Tokens in the administrator terminal')
    a=p.parse_args()
    from library_routes import load_routes
    path=Path(a.policy)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_SH)
        result=libraries(json.loads(path.read_text()),load_vault(path),load_routes(a.routes),a.library_id,a.show_tokens)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Token lookup failed; no secrets printed')
