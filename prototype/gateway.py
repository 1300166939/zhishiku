"""Loopback, single-principal context gateway. Scoped Token-authorized App report imports; common management remains CLI-only."""
import json
import sys
import threading
import uuid
import time
from diagnostics import event,failure
from authorization import Policy,Forbidden,audit,report_context,map_contexts,api_contexts,guard_job_read
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'integration'))
from ark_embedding import ArkEmbedding
from core import Knowledge, Qdrant
from rerank import configured as configured_reranker
from access import ContractError, domains, search_request, search_response, import_response, POST_ALIASES
QUERY_SLOTS=threading.BoundedSemaphore(2)
API_WRITE_SLOTS=threading.BoundedSemaphore(1)
QA_SLOTS=threading.BoundedSemaphore(1)


def settings(path='/etc/ai-knowledge-base/gateway.env'):
    return dict(line.split('=',1) for line in Path(path).read_text().splitlines() if '=' in line)


def knowledge(cfg, library='common', filters=None):
    if library=='test_api':
        from api_library import ApiKnowledge
        return ApiKnowledge(cfg['KB_API_DATA_ROOT'],ArkEmbedding.from_env(Path('/etc/ai-knowledge-base/model.env')),
            Qdrant(settings('/etc/ai-knowledge-base/qdrant.env')['QDRANT__SERVICE__API_KEY'],library='test_api',query_limit=50),
            cfg['KB_COLLECTOR_PRINCIPAL'],filters=filters,reranker=configured_reranker())
    if library == 'app':
        from app_library import AppKnowledge
        return AppKnowledge(cfg['KB_APP_DATA_ROOT'],ArkEmbedding.from_env(Path('/etc/ai-knowledge-base/model.env')),
            Qdrant(settings('/etc/ai-knowledge-base/qdrant.env')['QDRANT__SERVICE__API_KEY'],library='app'),
            cfg['KB_COLLECTOR_PRINCIPAL'],filters=filters,reranker=configured_reranker())
    if library != 'common' or filters is not None: raise ValueError('Invalid library route')
    return Knowledge(cfg['KB_DATA_ROOT'],ArkEmbedding.from_env(Path('/etc/ai-knowledge-base/model.env')),
                     Qdrant(settings('/etc/ai-knowledge-base/qdrant.env')['QDRANT__SERVICE__API_KEY']),
                     cfg['KB_COLLECTOR_PRINCIPAL'],reranker=configured_reranker())


def handler(kb, cfg):
    policy=Policy(cfg)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass  # Do not log source contents, query strings or Authorization headers.

        def reply(self,status,data):
            if getattr(self,'identity',None):
                if status in {401,403}:audit(self.identity,'request',None,'denied',request_id=getattr(self,'trace_id',None),reason=data.get('error'))
                elif status<300:
                    for domain,filters,prior in getattr(self,'read_checks',[]):audit(self.identity,'read',domain,'allowed',project=filters.get('project',prior),request_id=getattr(self,'trace_id',None))
            if status<300 and getattr(self,'identity',None):
                with policy.locked():
                    policy.credential(self.identity)
                    for domain,filters,prior in getattr(self,'read_checks',[]):
                        current,_=policy.read_scope(self.identity,domain,filters)
                        if current is not None and (prior is None or not set(prior)<=set(current)):raise Forbidden('authorization_changed')
                    payload=json.dumps(data,ensure_ascii=False).encode()
            else:payload=json.dumps(data,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)

        def authenticated(self):
            self.identity=policy.authenticate(self.headers.get('Authorization',''))
            self.read_checks=[]
            return self.identity is not None

        def write_authenticated(self):
            try:policy.scope(self.identity,'app','import');return True
            except Forbidden:return False

        def api_write_authenticated(self):
            for action in ['create','update']:
                try:policy.scope(self.identity,'test_api',action);return True
                except Forbidden:pass
            return False

        def selected(self,domain,filters=None):
            filters=filters or {}
            projects,version=policy.read_scope(self.identity,domain,filters)
            selected=kb if domain=='common' else knowledge(cfg,domain,filters)
            if domain!='common':selected.auth_projects=projects
            if domain!='common':
                from library_routes import load_routes,rerank_allowed,DEFAULT_PATH
                try:catalog=load_routes(cfg.get('KB_LIBRARY_ROUTES',DEFAULT_PATH))
                except Exception:catalog={'libraries':[]}
                selected.rerank_project_gate=lambda project:rerank_allowed(catalog,domain,project)
            self.read_checks.append((domain,filters,projects))
            return selected

        def job_read(self,jid,kind):
            ctx=guard_job_read(cfg,self.identity,jid,kind)
            projects,_=policy.scope(self.identity,'app')
            filters={'project':ctx['projects']} if all(ctx['projects']) else {}
            self.read_checks.append(('app',filters,projects))

        def forbidden(self,exc):
            audit(getattr(self,'identity',None),'request',None,'denied',request_id=getattr(self,'trace_id',None),reason=exc.code)
            self.reply(403,{'ok':False,'error':exc.code})

        def do_GET(self):
            self.trace_id=str(uuid.uuid4())
            try:self._do_GET()
            except Forbidden as exc:self.forbidden(exc)
            except Exception as exc:
                failure(exc,'authorization_read',request_id=self.trace_id)
                self.reply(503,{'error':'authorization_unavailable'})

        def _do_GET(self):
            if self.path=='/health':
                self.reply(200,{'status':'ok','scope':'isolated_prototype'});return
            if not self.authenticated():
                self.reply(401,{'error':'unauthorized'});return
            if self.path=='/domains':
                d=domains();allowed=[]
                for item in d['domains']:
                    try:policy.scope(self.identity,item['kb_domain']);allowed.append(item)
                    except Forbidden:pass
                d['domains']=allowed;self.reply(200,d);return
            if self.path=='/auth/me':
                self.reply(200,policy.public_identity(self.identity));return
            if self.path=='/libraries':
                from library_routes import load_routes,visible,DEFAULT_PATH
                try:
                    with policy.locked():
                        result=visible(policy,self.identity,load_routes(cfg.get('KB_LIBRARY_ROUTES',DEFAULT_PATH)))
                        self.reply(200,result)
                except Forbidden:raise
                except Exception:self.reply(503,{'error':'library_catalog_unavailable'})
                return
            if self.path=='/domains/test_api':
                policy.scope(self.identity,'test_api')
                self.reply(200,{'ok':True,**next(x for x in domains()['domains'] if x['kb_domain']=='test_api')});return
            if self.path.startswith('/imports/'):
                from app_jobs import status
                from app_maps import status as map_status
                jid=self.path.removeprefix('/imports/')
                mapped=map_status(cfg,jid)
                if mapped:
                    self.job_read(jid,'map');self.reply(200,mapped);return
                try:
                    result=status(cfg,jid);self.job_read(jid,'report');self.reply(200,import_response(result))
                except (ValueError,KeyError):self.reply(404,{'error':'job_not_found'})
                return
            if self.path.startswith('/v1/app/import-jobs/'):
                from app_jobs import status
                try:
                    jid=self.path.removeprefix('/v1/app/import-jobs/');result=status(cfg,jid)
                    self.job_read(jid,'report');self.reply(200,result)
                except (ValueError,KeyError):self.reply(404,{'error':'job_not_found'})
                return
            if self.path.startswith('/v1/assets/'):
                try:
                    aid=self.path.removeprefix('/v1/assets/')
                    selected=kb
                    if aid.startswith('app/'):
                        selected=knowledge(cfg,'app');aid=aid.removeprefix('app/')
                    domain='app' if selected is not kb else 'common'
                    with policy.locked():
                        projects,_=policy.scope(self.identity,domain)
                        if domain=='app' and projects is not None:
                            from app_search import project_field
                            with selected.db() as c:row=c.execute('SELECT '+project_field()+' FROM assets a JOIN app_entities e ON e.source_id=a.source_id AND e.version=a.version WHERE a.asset_id=?',(aid,)).fetchone()
                            if not row or row[0] not in projects:raise Forbidden('project_permission_denied')
                        data,mime=selected.asset(aid,cfg['KB_COLLECTOR_PRINCIPAL'])
                        self.send_response(200);self.send_header('Content-Type',mime)
                        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                except Forbidden:raise
                except (ValueError,PermissionError):
                    self.reply(404,{'error':'no_current_authorized_asset'})
                except Exception:
                    self.reply(503,{'error':'asset_unavailable'})
                return
            self.reply(404,{'error':'not_found'})

        def do_POST(self):
            self.trace_id=str(uuid.uuid4());started=time.monotonic()
            if self.path=='/ask':
                if not QA_SLOTS.acquire(blocking=False):
                    self.reply(503,{'error':'qa_busy','retry_after_seconds':2});return
                try:self._do_POST()
                except Forbidden as exc:self.forbidden(exc)
                except Exception as exc:
                    failure(exc,'qa_request',request_id=self.trace_id)
                    self.reply(503,{'error':'qa_unavailable'})
                finally:QA_SLOTS.release()
                return
            bounded=self.path in {'/search','/batch_search','/rerank','/search_image','/reports/app','/maps/app','/v1/agent-context/query','/v1/agent-context/query-image','/v1/app/reports'}
            acquired=not bounded or QUERY_SLOTS.acquire(blocking=False)
            if not acquired:
                self.reply(503,{'error':'query_busy','retry_after_seconds':2,'request_id':self.trace_id});return
            try:self._do_POST()
            except Forbidden as exc:self.forbidden(exc)
            except Exception as exc:
                failure(exc,'authorization_read',request_id=self.trace_id)
                self.reply(503,{'error':'authorization_unavailable'})
            finally:
                if bounded:QUERY_SLOTS.release()
                event('http_request',request_id=self.trace_id,path=self.path,elapsed_ms=round((time.monotonic()-started)*1000))

        def _do_POST(self):
            if self.path=='/libraries':
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=8192:self.reply(413,{'error':'body_size_limit'});return
                    body=json.loads(self.rfile.read(length))
                    from library_operator import gateway_call,SOCKET
                    code,result=gateway_call(self.headers.get('Authorization',''),body,cfg.get('KB_LIBRARY_OPERATOR_SOCKET',SOCKET))
                    self.reply(code,result)
                except (ValueError,json.JSONDecodeError):self.reply(400,{'error':'invalid_library_request'})
                except Exception:self.reply(503,{'error':'library_operator_unavailable'})
                return
            if not self.authenticated():
                self.reply(401,{'error':'unauthorized'});return
            path=POST_ALIASES.get(self.path,self.path)
            alias=path!=self.path
            if path not in {'/ask','/upsert','/imports/api/items','/batch_search','/rerank','/imports/app/maps','/maps/app','/search','/search_image','/v1/agent-context/query','/v1/agent-context/query-image','/v1/app/reports','/v1/app/import-jobs','/v1/app/report-packages'}:
                self.reply(404,{'error':'not_found'});return
            try:
                length=int(self.headers.get('Content-Length','0'))
                if path=='/v1/app/report-packages':
                    if not self.write_authenticated():self.reply(403,{'error':'write_token_required'});return
                    from app_jobs import MAX_PACKAGE_BYTES
                    if not 0<length<=MAX_PACKAGE_BYTES:self.reply(413,{'error':'package_size_limit'});return
                    import uuid
                    from app_jobs import accept_package
                    import zipfile
                    folder=Path(cfg['KB_APP_DATA_ROOT'])/'packages';folder.mkdir(exist_ok=True,mode=0o700)
                    path=folder/(str(uuid.uuid4())+'.zip');remaining=length
                    try:
                        with path.open('xb') as out:
                            while remaining:
                                block=self.rfile.read(min(1048576,remaining))
                                if not block:raise ValueError('Incomplete package')
                                out.write(block);remaining-=len(block)
                        job=accept_package(cfg,path,credential_id=self.identity)
                        self.reply(202,import_response(job) if alias else job);return
                    except zipfile.BadZipFile:
                        path.unlink(missing_ok=True);raise ValueError('Invalid report package')
                    except Exception:
                        path.unlink(missing_ok=True);raise
                limit=2_000_000 if path in {'/upsert','/imports/api/items','/batch_search','/rerank'} else (1_000_000 if path=='/imports/app/maps' else (2_000_000 if path.endswith('query-image') or path=='/search_image' else 16384))
                if not 0<length<=limit:
                    self.reply(413,{'error':'body_size_limit'});return
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict):raise ValueError('Invalid request')
                input_items=body.get('items',[]) if isinstance(body.get('items',[]),list) else []
                is_api=path=='/imports/api/items' or (path=='/upsert' and (any(isinstance(i,dict) and isinstance(i.get('metadata'),dict) and i['metadata'].get('kb_domain')=='test_api' for i in input_items) or (self.api_write_authenticated() and not self.write_authenticated())))
                if is_api:
                    if not self.api_write_authenticated():self.reply(403,{'ok':False,'error':'api_write_token_required'});return
                    from api_library import validate_items
                    items=validate_items(body)
                    if not API_WRITE_SLOTS.acquire(blocking=False):self.reply(503,{'ok':False,'error':'api_write_busy'});return
                    try:
                        import fcntl
                        selected=knowledge(cfg,'test_api')
                        contexts=api_contexts(policy,self.identity,selected,items)
                        with (selected.root/'import.lock').open('a') as lock:
                            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                            except BlockingIOError:self.reply(503,{'ok':False,'error':'api_write_busy'});return
                            def commit():
                                # Re-read stored ownership after model work while publication is serialized.
                                fresh=api_contexts(policy,self.identity,selected,items)
                                return policy.commit(fresh)
                            result=selected.upsert(items,approval=self.identity,approval_ref=self.trace_id,commit_guard=commit)
                        audit(self.identity,'upsert','test_api','allowed',resource=str(len(items)),request_id=self.trace_id)
                        self.reply(200,result)
                    finally:API_WRITE_SLOTS.release()
                    return
                if path=='/batch_search':
                    tasks=body.get('tasks')
                    if set(body)!={'tasks'} or not isinstance(tasks,list) or not 0<=len(tasks)<=20:raise ValueError('Invalid batch tasks')
                    validated=[search_request(task) for task in tasks];results=[]
                    for domain,filters,top_k in validated:policy.read_scope(self.identity,domain,filters)
                    for task,(domain,filters,top_k) in zip(tasks,validated):
                        selected=self.selected(domain,filters)
                        result=selected.query(task.get('query'),cfg['KB_COLLECTOR_PRINCIPAL'],top_k)
                        results.extend(search_response(result,domain)['results'])
                    self.reply(200,{'ok':True,'task_count':len(tasks),'results':results});return
                if path=='/rerank':
                    policy.credential(self.identity)
                    from library_routes import load_routes,credential_rerank_allowed,DEFAULT_PATH
                    try:enabled=credential_rerank_allowed(policy,self.identity,load_routes(cfg.get('KB_LIBRARY_ROUTES',DEFAULT_PATH)))
                    except Forbidden:raise
                    except Exception:enabled=False
                    if not enabled:raise Forbidden('rerank_not_enabled_for_library')
                    from rerank import RerankError
                    query=body.get('query');items=body.get('items')
                    if set(body)!={'query','items'} or not isinstance(query,str) or not query.strip() or len(query)>2000 or not isinstance(items,list) or len(items)>50:raise ValueError('Invalid rerank request')
                    if any(not isinstance(i,dict) or set(i)!={'text'} or not isinstance(i['text'],str) or len(i['text'])>6000 for i in items):raise ValueError('Invalid rerank items')
                    if not items:self.reply(200,{'ok':True,'model':'qwen3-rerank','scores':[]});return
                    client=configured_reranker()
                    if not client or not client.enabled:self.reply(503,{'ok':False,'error':'rerank_disabled'});return
                    docs=[i['text'][:min(1500,30000//len(items))] for i in items]
                    try:values=client.score(query,docs)
                    except RerankError as exc:self.reply(503,{'ok':False,'error':'rerank_'+exc.code});return
                    self.reply(200,{'ok':True,'model':client.model,'scores':[values[i] for i in range(len(items))]});return
                if path in {'/upsert','/imports/app/maps'}:
                    if not self.write_authenticated():self.reply(403,{'ok':False,'error':'write_token_required'});return
                    from app_maps import submit,MapConflict,validate
                    items,_=validate(body);contexts=map_contexts(policy,self.identity,knowledge(cfg,'app'),items)
                    try:
                        with policy.commit(contexts):self.reply(202,submit(cfg,body,auth_context=contexts[0]))
                    except MapConflict as exc:self.reply(409,{'ok':False,'error':str(exc)})
                    return
                if path=='/maps/app':
                    from app_maps import listing
                    self.reply(200,listing(self.selected('app',body.get('filters')),body));return
                if path=='/ask':
                    from evidence_qa import query as evidence_query
                    domain,filters,top_k=search_request(body)
                    selected=self.selected(domain,filters)
                    result=evidence_query(selected,body.get('query'),cfg['KB_COLLECTOR_PRINCIPAL'],top_k)
                    result['kb_domain']=domain
                    self.reply(200,result);return
                if path in {'/search','/search_image'}:
                    image=path=='/search_image'
                    domain,filters,top_k=search_request(body,image=image)
                    selected=self.selected(domain,filters)
                    if image:
                        import base64
                        data=base64.b64decode(body.get('image_base64',''),validate=True)
                        result=selected.query_image(data,body.get('mime'),cfg['KB_COLLECTOR_PRINCIPAL'],top_k)
                    else:result=selected.query(body.get('query'),cfg['KB_COLLECTOR_PRINCIPAL'],top_k)
                    self.reply(200,search_response(result,domain));return
                if path=='/v1/app/import-jobs':
                    if not self.write_authenticated():self.reply(403,{'error':'write_token_required'});return
                    from app_jobs import submit
                    from app_jobs import validate_url
                    url=validate_url(body.get('report_url'))
                    context=report_context(cfg,self.identity,url,body.get('project'))
                    job=submit(cfg,body,auth_context=context)
                    self.reply(202,import_response(job) if alias else job);return
                if path=='/v1/app/reports':
                    if set(body)-{'filters'}:raise ValueError('Invalid report fields')
                    self.reply(200,self.selected('app',body.get('filters')).reports(body.get('filters')));return
                library=body.get('library','common')
                if library not in {'common','app','test_api'}:raise ValueError('Invalid library')
                if library=='common' and body.get('filters'):raise ValueError('Invalid common filters')
                selected=self.selected(library,body.get('filters'))
                if path.endswith('query-image'):
                    import base64
                    if not isinstance(body,dict) or set(body)-{'image_base64','mime','top_k','library','filters'}:
                        raise ValueError('Invalid image fields')
                    data=base64.b64decode(body.get('image_base64',''),validate=True)
                    self.reply(200,selected.query_image(data,body.get('mime'),cfg['KB_COLLECTOR_PRINCIPAL'],body.get('top_k',5)))
                    return
                if not isinstance(body,dict) or set(body)-{'query','top_k','library','filters'}:
                    raise ValueError('Invalid query fields')
                self.reply(200,selected.query(body.get('query'),cfg['KB_COLLECTOR_PRINCIPAL'],body.get('top_k',5)))
            except Forbidden as exc:self.forbidden(exc)
            except ContractError as exc:
                self.reply(400,{'ok':False,'error':str(exc)})
            except (ValueError,TypeError) as exc:
                failure(exc,'request_validation',request_id=self.trace_id,path=self.path)
                self.reply(400,{'error':'invalid_request'})
            except Exception as exc:
                failure(exc,'http_request',request_id=self.trace_id,path=self.path)
                self.reply(503,{'error':'retrieval_unavailable'})

    return Handler


if __name__=='__main__':
    cfg=settings()
    if cfg.get('KB_APP_WRITE_TOKEN'):
        from app_jobs import resume_pending
        resume_pending(cfg)
        from app_maps import resume
        resume(cfg)
    class Server(ThreadingHTTPServer):
        def get_request(self):
            socket,address=super().get_request();socket.settimeout(60);return socket,address
    Server(('127.0.0.1',8088),handler(knowledge(cfg),cfg)).serve_forever()
