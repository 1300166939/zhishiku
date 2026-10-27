"""Administrator CLI: register a project-scoped library, with rerank disabled by default."""
import argparse,fcntl,json,os,secrets
from pathlib import Path
from authorization import ID,digest,validate_policy
from library_routes import load_routes
from manage_credentials import write_atomic
from shared_readers import load_registry,registry_path,register
from credential_vault import load_vault,vault_path,retain

def create(policy_path,routes_path,library_id,project,name,owner,output_dir=None,domain='app',enrollment=None):
    policy_path=Path(policy_path);routes_path=Path(routes_path)
    if domain not in {'app','test_api'} or not ID.fullmatch(library_id) or not ID.fullmatch(project) or not name or not owner:raise ValueError('Invalid library configuration')
    output_dir=Path(output_dir) if output_dir else policy_path.parent/'issued'/library_id
    with policy_path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        policy=validate_policy(json.loads(policy_path.read_text()));routes=load_routes(routes_path);registry=load_registry(policy_path)
        if any(r['library_id']==library_id or (r['kb_domain']==domain and r['project']==project) for r in routes['libraries']):raise ValueError('Library or project already registered')
        ids=[library_id+'-reader',library_id+'-writer']
        if any(not ID.fullmatch(cid) or any(c['credential_id']==cid for c in policy['credentials']) for cid in ids):raise ValueError('Credential id conflict')
        tokens=['kb_'+secrets.token_urlsafe(32) for _ in ids]
        for cid,token,actions in zip(ids,tokens,[['read'],['read','create','update','import'] if domain=='app' else ['read','create','update']]):
            policy['credentials'].append({'credential_id':cid,'owner':owner,'enabled':True,'token_digest':digest(token),'grants':[{'domain':domain,'projects':[project],'actions':actions,'default_project':project}]})
        policy['version']+=1;validate_policy(policy)
        registry=register(policy,registry,ids[0],tokens[0],name+'只读')
        vault=load_vault(policy_path)
        for cid,token in zip(ids,tokens):vault=retain(policy,vault,cid,token)
        row={'library_id':library_id,'name':name,'owner':owner,'kb_domain':domain,'project':project,'enabled':True,'read_credential_id':ids[0],'write_credential_id':ids[1],'rerank_enabled':False}
        if enrollment:row.update(enrollment)
        routes['libraries'].append(row)
        paths=[policy_path,registry_path(policy_path),routes_path,vault_path(policy_path)];before={p:(json.loads(p.read_text()) if p.exists() else None) for p in paths}
        created=[]
        try:
            output_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
            if output_dir.stat().st_mode&0o077:raise ValueError('Token directory must be private')
            for cid,token in zip(ids,tokens):
                file=output_dir/(cid+'.env');fd=os.open(file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);created.append(file)
                with os.fdopen(fd,'w') as f:f.write('KB_TOKEN='+token+'\n');f.flush();os.fsync(f.fileno())
            # Exporters/commits take policy.lock. Publish authorization last, so
            # the new tokens cannot work before their route is available.
            # Handled failures roll back; this is not a durable crash transaction.
            write_atomic(paths[1],registry,0o600);write_atomic(paths[3],vault,0o600);write_atomic(routes_path,routes);load_routes(routes_path);write_atomic(policy_path,policy)
        except Exception:
            for p,value in before.items():
                if value is None:p.unlink(missing_ok=True)
                else:write_atomic(p,value,0o600 if p in [paths[1],paths[3]] else None)
            for p in created:p.unlink(missing_ok=True)
            raise
        return {'ok':True,'library_id':library_id,'project':project,'kb_domain':domain,'read_credential_id':ids[0],'write_credential_id':ids[1],'rerank_enabled':False,'token_files':[str(p) for p in created],'token_vault':str(vault_path(policy_path))}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--policy',default='/var/lib/ai-knowledge-base/access/policy.json');p.add_argument('--routes',default='/var/lib/ai-knowledge-base/access/library-routes.json')
    for key in ['library-id','project','name','owner']:p.add_argument('--'+key,required=True)
    p.add_argument('--output-dir',help='Optional extra Token file directory; central cloud vault is always retained')
    p.add_argument('--domain',choices=['app','test_api'],default='app');a=p.parse_args()
    print(json.dumps(create(a.policy,a.routes,a.library_id,a.project,a.name,a.owner,a.output_dir,a.domain),ensure_ascii=False))
if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Library registration failed; no secrets printed')
