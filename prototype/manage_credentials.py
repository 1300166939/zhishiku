"""Operator-only local CLI. Tokens go to a new 0600 file, never stdout."""
import argparse,fcntl,json,os,secrets,tempfile
from pathlib import Path
from authorization import validate_policy,digest
from shared_readers import register,load_registry,registry_path,only_read
from credential_vault import load_vault,vault_path,retain

def write_atomic(path,data,mode=None):
    path=Path(path);fd,name=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        if path.exists():
            stat=path.stat();os.fchown(fd,stat.st_uid,stat.st_gid)
        os.fchmod(fd,mode if mode is not None else (path.stat().st_mode&0o777 if path.exists() else 0o600))
        with os.fdopen(fd,'w') as f:f.write(json.dumps(data,ensure_ascii=False,indent=2)+'\n');f.flush();os.fsync(f.fileno())
        os.replace(name,path)
        d=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(d)
        finally:os.close(d)
    finally:
        if os.path.exists(name):os.unlink(name)

def change(path,operation,cid,grant=None,token_file=None,owner=None,share_read=False,label=None,default_read=False):
    path=Path(path)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        policy=validate_policy(json.loads(path.read_text()))
        target=next((c for c in policy['credentials'] if c['credential_id']==cid),None)
        registry=load_registry(path);old_registry=json.loads(json.dumps(registry));changed_registry=False
        vault=load_vault(path);old_vault=json.loads(json.dumps(vault));changed_vault=False
        if operation=='issue':
            if target:raise ValueError('Credential ID already exists')
            token='kb_'+secrets.token_urlsafe(32)
            policy['credentials'].append({'credential_id':cid,'owner':owner or cid,'enabled':True,'token_digest':digest(token),'grants':grant})
        elif operation in {'enable','disable'}:
            if target is None:raise ValueError('Credential not found')
            target['enabled']=operation=='enable'
        elif operation=='grant':
            if target is None:raise ValueError('Credential not found')
            target['grants']=grant
        elif operation in {'share-read','rotate'}:
            if target is None or not only_read(target):raise ValueError('Shared credential must be read-only')
            if operation=='share-read':
                token=Path(token_file).read_text().strip().split('=',1)[1]
            else:
                if not any(r['credential_id']==cid for r in registry['credentials']):raise ValueError('Rotate supports registered shared readers only')
                token='kb_'+secrets.token_urlsafe(32);target['token_digest']=digest(token)
                prior=next(r for r in registry['credentials'] if r['credential_id']==cid);label=label or prior['label'];default_read=prior.get('default',False)
        else:raise ValueError('Invalid operation')
        if share_read or operation in {'share-read','rotate'}:
            registry=register(policy,registry,cid,token,label,default_read);changed_registry=True
        policy['version']+=1;validate_policy(policy)
        if operation=='rotate' and any(r['credential_id']==cid for r in vault['credentials']):
            vault=retain(policy,vault,cid,token);changed_vault=True
        if operation in {'issue','rotate'}:
            # Fail if an output exists; no accidental overwrite of another credential.
            fd=os.open(token_file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'w') as f:f.write('KB_TOKEN='+token+'\n');f.flush();os.fsync(f.fileno())
        try:
            # Publication registry first; exporter suppresses entries not matching current policy.
            if changed_registry:write_atomic(registry_path(path),registry,0o600)
            if changed_vault:write_atomic(vault_path(path),vault,0o600)
            write_atomic(path,policy)
        except Exception:
            if changed_registry:write_atomic(registry_path(path),old_registry,0o600)
            if changed_vault:write_atomic(vault_path(path),old_vault,0o600)
            if operation in {'issue','rotate'}:Path(token_file).unlink(missing_ok=True)
            raise
        return {'ok':True,'credential_id':cid,'operation':operation,'policy_version':policy['version']}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--policy',required=True,type=Path)
    p.add_argument('operation',choices=['list','issue','grant','enable','disable','share-read','rotate']);p.add_argument('--id');p.add_argument('--owner')
    p.add_argument('--share-read',action='store_true');p.add_argument('--label');p.add_argument('--default-read',action='store_true')
    p.add_argument('--grants-file',type=Path);p.add_argument('--token-file',type=Path);args=p.parse_args()
    if args.operation=='list':
        policy=validate_policy(json.loads(args.policy.read_text()))
        print(json.dumps({'version':policy['version'],'credentials':[{k:v for k,v in c.items() if k!='token_digest'} for c in policy['credentials']]},ensure_ascii=False));return
    if not args.id or (args.operation in {'issue','grant'} and not args.grants_file) or (args.operation in {'issue','share-read','rotate'} and not args.token_file):p.error('Missing id, grants-file or token-file')
    if args.share_read and args.operation!='issue':p.error('--share-read only applies to issue')
    grants=json.loads(args.grants_file.read_text()) if args.grants_file else None
    print(json.dumps(change(args.policy,args.operation,args.id,grants,args.token_file,args.owner,args.share_read,args.label,args.default_read),ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,json.JSONDecodeError):raise SystemExit('Credential operation failed; no secrets printed')
