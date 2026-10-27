"""Fixed-function root operator over a local Unix socket; only self-service creation."""
import argparse,fcntl,hashlib,hmac,json,os,pwd,re,socket,socketserver,struct,time,uuid
from pathlib import Path
from authorization import ID,digest
from register_private_library import create
from library_routes import load_routes
from credential_vault import load_vault,libraries
from diagnostics import event,failure

SOCKET='/run/ai-kb-operator/operator.sock'
class Denied(Exception):pass
class Conflict(Exception):pass

def provision(access,token,body):
    access=Path(access)
    if not isinstance(token,str) or len(token)>4096:raise Denied()
    if not isinstance(body,dict) or set(body)!={'library_id','project','name','owner','kb_domain','request_id'}:raise ValueError('Invalid fields')
    for key in ['library_id','project','request_id']:
        if not isinstance(body[key],str) or not ID.fullmatch(body[key]):raise ValueError('Invalid identifier')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',body['library_id']):raise ValueError('Invalid library slug')
    # An unpublished random request ID is the replay receipt for a shared creator.
    receipt=uuid.UUID(body['request_id'])
    if receipt.version!=4 or str(receipt)!=body['request_id']:raise ValueError('Expected random UUID4 request receipt')
    for key in ['name','owner']:
        if not isinstance(body[key],str) or not body[key].strip() or len(body[key])>256:raise ValueError('Invalid description')
    if body['kb_domain'] not in ['app','test_api']:raise ValueError('Invalid domain')
    # Serialize creator admission, capacity and idempotency; create takes policy.lock.
    with (access/'enrollment.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        path=access/'enrollment.json'
        if path.stat().st_mode&0o077:raise RuntimeError('Enrollment registry must be private')
        data=json.loads(path.read_text());target=digest(token)
        creator=next((c for c in data['creators'] if hmac.compare_digest(c['token_digest'],target)),None)
        if not creator or not creator['enabled'] or (creator.get('expires_at') is not None and creator['expires_at']<=time.time()):raise Denied()
        if body['kb_domain'] not in creator['domains']:raise Denied()
        if type(creator['max_libraries']) is not int or not 1<=creator['max_libraries']<=100:raise RuntimeError('Invalid creator capacity')
        cid=creator['credential_id'];policy=access/'policy.json';routes=access/'library-routes.json';catalog=load_routes(routes)
        fingerprint=hashlib.sha256(json.dumps(body,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        prior=next((r for r in catalog['libraries'] if r.get('created_by')==cid and r.get('creation_request_id')==body['request_id']),None)
        replay=bool(prior)
        if prior:
            if prior.get('creation_request_hash')!=fingerprint:raise Conflict()
            library_id=prior['library_id']
        else:
            if any(r['library_id']==body['library_id'] or (r['kb_domain']==body['kb_domain'] and r.get('project')==body['project']) for r in catalog['libraries']):raise Conflict()
            if sum(r.get('created_by')==cid for r in catalog['libraries'])>=creator['max_libraries']:raise Denied()
            library_id=body['library_id']
            create(policy,routes,library_id,body['project'],body['name'].strip(),body['owner'].strip(),domain=body['kb_domain'],enrollment={'created_by':cid,'creation_request_id':body['request_id'],'creation_request_hash':fingerprint})
        with policy.with_suffix('.lock').open('a') as p:
            fcntl.flock(p,fcntl.LOCK_SH)
            item=libraries(json.loads(policy.read_text()),load_vault(policy),load_routes(routes),library_id,True)['libraries'][0]
        if any(not item[role]['saved'] or not item[role]['active'] for role in ['read','write']):raise Denied()
        event('library_self_service',credential_id=cid,library_id=library_id,domain=item['kb_domain'],project=item['project'],result='replayed' if replay else 'created')
        return {'ok':True,'library_id':library_id,'project':item['project'],'kb_domain':item['kb_domain'],'rerank_enabled':item['rerank_enabled'],'read_token':item['read']['token'],'write_token':item['write']['token'],'read_credential_id':item['read']['credential_id'],'write_credential_id':item['write']['credential_id'],'replayed':replay,'query_path':'/search','token_saved_on_cloud':True}

def gateway_call(header,body,path=SOCKET):
    if not isinstance(header,str) or not header.startswith('Bearer '):return 401,{'error':'unauthorized'}
    packet=json.dumps({'token':header[7:],'body':body},ensure_ascii=False).encode()+b'\n'
    if len(packet)>16384:return 413,{'error':'body_size_limit'}
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
        s.settimeout(15);s.connect(str(path));s.sendall(packet);raw=b''
        while not raw.endswith(b'\n'):
            part=s.recv(32768)
            if not part or len(raw)+len(part)>32768:raise RuntimeError('Invalid operator response')
            raw+=part
    result=json.loads(raw)
    return result['status'],result['data']

def serve(access,socket_path=SOCKET,allowed_uid=None):
    path=Path(socket_path);account=pwd.getpwnam('aikb') if allowed_uid is None else None
    uid=account.pw_uid if account else allowed_uid
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(10)
            peer=struct.unpack('3i',self.request.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]
            if peer not in [0,uid]:return
            status=503;data={'error':'library_operator_unavailable'}
            try:
                raw=self.rfile.readline(16385)
                if len(raw)>16384 or not raw.endswith(b'\n'):raise ValueError()
                packet=json.loads(raw)
                if set(packet)!={'token','body'}:raise ValueError()
                data=provision(access,packet['token'],packet['body']);status=200 if data['replayed'] else 201
            except Denied:status=403;data={'error':'library_creator_required_or_limit'}
            except Conflict:status=409;data={'error':'library_or_request_conflict'}
            except ValueError:status=400;data={'error':'invalid_library_request'}
            except Exception as exc:failure(exc,'library_operator')
            self.wfile.write(json.dumps({'status':status,'data':data},ensure_ascii=False).encode()+b'\n')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o750)
    path.unlink(missing_ok=True)
    with socketserver.UnixStreamServer(str(path),Handler) as server:
        if account:os.chown(path,0,account.pw_gid);os.chown(path.parent,0,account.pw_gid)
        path.chmod(0o660);server.serve_forever()
def main():
    p=argparse.ArgumentParser();p.add_argument('--access',default='/var/lib/ai-knowledge-base/access');p.add_argument('--socket',default=SOCKET);a=p.parse_args();serve(a.access,a.socket)
if __name__=='__main__':main()
