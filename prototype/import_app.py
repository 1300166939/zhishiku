"""Trusted, resumable App import; user-authorized report, no public write API."""
import argparse
import base64
import copy
import concurrent.futures
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from gateway import knowledge,settings
from diagnostics import failure

PROFILE='app-chunk-v1:semantic-fields:max1200:ark2048'

def report_profile(report):
    if report.get('storage_mode')=='links_only':
        return 'app-links-'+report['parser_version'].rsplit('-',1)[1]+':semantic-fields:max1200:ark2048'
    if report.get('parser_version')=='app-html-v1':return PROFILE
    if report.get('parser_version')=='app-html-v2':return 'app-chunk-v2:step-occurrence:semantic-fields:max1200:ark2048'
    raise ValueError('Explicit parser/profile migration required')

def report_version(report):
    if report.get('publication_version'):return report['publication_version']
    return report['html_sha256'][:16]+'-'+report_profile(report).split(':')[0]

def normalized_run(run):
    """Compare execution content independently of its single/summary HTML wrapper."""
    run=copy.deepcopy(run)
    run['case_key']=run['case_id']+'__'+run['platform']+'__'+run['run_id']
    attempt=1;previous=None
    for ordinal,step in enumerate(run['steps'],1):
        original=step.get('source_step_index',step['step_index'])
        if previous is not None and original<=previous:attempt+=1
        step.update(step_index=ordinal,source_step_index=original,attempt_index=attempt,
                    step_id=run['run_id']+':'+str(ordinal))
        step['screenshot_slots']=step.get('screenshot_slots',[{**x,'status':'present'} for x in step['screenshots']])
        previous=original
    run['attempt_count']=attempt
    return run

def prepare_report(kb,incoming,raw):
    """Merge observed runs, preserving absent runs and unchanged legacy versions."""
    report=copy.deepcopy(incoming);batch=report['batch_id']
    incoming_ids={r['run_id'] for r in incoming['runs']}
    with kb.db() as c:
        row=c.execute("SELECT * FROM app_reports WHERE batch_id=? AND status='published'",(batch,)).fetchone()
        entities={r['source_id']:r['version'] for r in c.execute("SELECT e.source_id,e.version FROM app_entities e JOIN sources s USING(source_id) WHERE s.active_version=e.version AND json_extract(e.metadata,'$.batch_id')=?",(batch,))}
    old=json.loads(kb.objects.get(row['manifest_ref'])) if row else None
    runs={r['run_id']:r for r in old['runs']} if old else {}
    versions={};unchanged=set()
    for run in report['runs']:
        rid=run['run_id'];normalized=normalized_run(run)
        if rid in runs and normalized_run(runs[rid])==normalized:
            unchanged.add(rid)
        else:runs[rid]=normalized
    for rid,run in runs.items():
        prefix='app:'+batch+':'+rid
        sids=[prefix+':summary',prefix+':definition']+[prefix+':step:'+str(s['step_index']) for s in run['steps']]
        if (rid in unchanged or rid not in incoming_ids) and all(sid in entities for sid in sids):
            versions.update({sid:entities[sid] for sid in sids})
        else:
            digest=hashlib.sha256(json.dumps(normalized_run(run),ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            versions.update({sid:digest[:24]+'-app-run-v3' for sid in sids})
    report['runs']=[runs[rid] for rid in sorted(runs)]
    report.update(title='App执行批次 '+batch,parser_version='app-html-v2',storage_mode='links_only',
                  step_index_semantics='chronological_occurrence_within_run')
    urls=[s['url'] for r in report['runs'] for step in r['steps'] for s in step['screenshots']]
    report['counts']={'runs':len(runs),'steps':sum(len(r['steps']) for r in report['runs']),
        'logs':sum(len(r['logs']) for r in report['runs']),'screenshot_references':len(urls),
        'screenshot_urls':len(set(urls)),'declared_steps':sum(r['declared_steps'] for r in report['runs']),
        'missing_screenshot_slots':sum(s['status']=='missing' for r in report['runs'] for step in r['steps'] for s in step.get('screenshot_slots',[])),
        'restarted_runs':sum(r.get('attempt_count',1)>1 for r in report['runs'])}
    if report['counts']['steps']>2500 or report['counts']['screenshot_urls']>5000:
        raise ValueError('Merged report capacity limit')
    report['coverage_mismatches']=[{'case_id':r['case_id'],'run_id':r['run_id'],'declared':r['declared_steps'],'rendered':len(r['steps'])}
        for r in report['runs'] if r['declared_steps']!=len(r['steps'])]
    digest=hashlib.sha256(json.dumps({'runs':report['runs'],'versions':versions,'profile':report_profile(report)},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    report['publication_version']=digest[:24]+'-app-batch-v3'
    versions['app:'+batch+':batch']=report['publication_version']
    report['entity_versions']=versions
    report['previous_publication']=row['version'] if row else None
    provenance=copy.deepcopy(old.get('input_reports',[])) if old else []
    if old and not provenance:
        provenance=[{'url':old['source_url'],'html_sha256':old['html_sha256'],'html_ref':row['html_ref']}]
    item={'url':incoming['source_url'],'html_sha256':incoming['html_sha256'],'html_ref':kb.objects.put(raw)}
    if item not in provenance:provenance.append(item)
    report['input_reports']=provenance
    return report

def chunk_fields(fields,block):
    out=[];current=''
    for field in fields:
        # Split at original line/paragraph boundaries; oversized line retains identity prefix in every fragment.
        for line in field.splitlines():
            for i in range(0,max(1,len(line)),1100):
                part=line[i:i+1100]
                if len(current)+len(part)>1200:
                    out.append({'text':current,'block_id':block});current=''
                current+=part+'\n'
    if current.strip():out.append({'text':current.strip(),'block_id':block})
    return out

class CacheEmbedding:
    """Cache only content/profile vectors; every evidence point keeps its own source/version."""
    def __init__(self,kb,profile=PROFILE):
        self.kb=kb;self.real=kb.embedding.real if isinstance(kb.embedding,CacheEmbedding) else kb.embedding
        self.profile=profile
        self.model_calls=0
        if self.real.model!='doubao-embedding-vision-251215' or self.real.dimensions!=2048:
            raise ValueError('App vector profile migration required for changed model/dimension')
        self.guard=threading.Lock();self.locks={}
        with kb.db() as c:c.execute('CREATE TABLE IF NOT EXISTS app_vector_cache(cache_key TEXT PRIMARY KEY, object_ref TEXT)')
    def embed(self,kind,data,mime=None):
        key=hashlib.sha256((self.profile+':'+kind+':'+str(mime)).encode()+data).hexdigest()
        with self.guard:lock=self.locks.setdefault(key,threading.Lock())
        with lock:
            with self.kb.db() as c:row=c.execute('SELECT object_ref FROM app_vector_cache WHERE cache_key=?',(key,)).fetchone()
            if row:return SimpleNamespace(vector=json.loads(self.kb.objects.get(row[0])))
            for attempt in range(3):
                try:
                    result=self.real.embed_text(data.decode()) if kind=='text' else self.real.embed_image(data,mime=mime)
                    self.model_calls+=1
                    break
                except Exception:
                    if attempt==2:raise
                    time.sleep(2**attempt)
            ref=self.kb.objects.put(json.dumps(result.vector).encode())
            with self.kb.db() as c:c.execute('INSERT OR REPLACE INTO app_vector_cache VALUES(?,?)',(key,ref))
            return result
    def embed_text(self,text):return self.embed('text',text.encode())
    def embed_image(self,data,mime):return self.embed('image',data,mime)

def records(report,downloads,objects_root=None):
    objects_root=Path(objects_root or "/var/lib/ai-knowledge-base/app/objects")
    version=report_version(report)
    links=report.get('storage_mode')=='links_only'
    base={'batch_id':report['batch_id'],'platform':'Android','app_build':'unknown','environment':'unknown','review_status':'unreviewed'}
    if links:base['storage_mode']='links_only'
    yield ('app:'+report['batch_id']+':batch',version,{**base,'entity_kind':'batch_summary'},report,
        {'chunks':chunk_fields([report['title'],json.dumps(report['counts'],ensure_ascii=False),
          '报告标记成功'+str(sum(r['reported_status']=='success' for r in report['runs']))+'，失败'+str(sum(r['reported_status']=='failed' for r in report['runs']))+'；未评审，不代表确认缺陷。',json.dumps(report['coverage_mismatches'],ensure_ascii=False)],'batch'),'media':[]})
    for run in report['runs']:
        meta={**base,**{k:run[k] for k in ['case_id','run_id','case_key','reported_status','reported_reason','platform']}}
        prefix=f"{run['case_id']} / {run['run_id']} / {run['title']}"
        source='app:'+report['batch_id']+':'+run['run_id']
        run_version=report.get('entity_versions',{}).get(source+':summary',version)
        yield (source+':summary',run_version,{**meta,'entity_kind':'case_run_summary'},run,
            {'chunks':chunk_fields([prefix,'执行器报告状态：'+run['reported_status']+'；原因：'+run['reported_reason']+'；人工评审：unreviewed',
              '声明步骤'+str(run['declared_steps'])+'；展示步骤'+str(run['rendered_steps']),run['failure_summary']], 'summary'),'media':[]})
        yield (source+':definition',report.get('entity_versions',{}).get(source+':definition',version),{**meta,'entity_kind':'case_definition'},run,
            {'chunks':chunk_fields([prefix,run['definition']],'definition'),'media':[]})
        for step in run['steps']:
            n=step['step_index'];logs=[run['logs'][i] for i in step['log_indices']]
            occurrence={k:step[k] for k in ['source_step_index','attempt_index'] if k in step}
            # Retain all logs in parent object. Retrieval excludes recurring heartbeats/goal replay.
            relevant=[l['time']+' '+l['text'] for l in logs if re.search(r'动作|执行结果|错误|异常|断言|assert|error|失败|成功|action|tap|swipe|click|输入|第\s*\d+\s*步',l['text'],re.I)]
            if not relevant:relevant=[l['time']+' '+l['text'] for l in logs[-2:]]
            media=[]
            for shot in step['screenshots']:
                if links:continue
                item=downloads[shot['url']]
                if item['status']!='downloaded':raise ValueError('Screenshot download incomplete')
                data=objects_root/item['object_ref'][:2]/item['object_ref']
                media.append({'data_base64':base64.b64encode(data.read_bytes()).decode(),'mime':item['mime'],
                    'kind':shot['role'],'coverage':'original_report_screenshot','block_id':'step-'+str(n)})
            slot_text=['原步骤 '+str(step['source_step_index'])+' / 尝试 '+str(step['attempt_index']),
                       '截图槽：'+json.dumps(step['screenshot_slots'],ensure_ascii=False)] if occurrence else []
            yield (source+':step:'+str(n),report.get('entity_versions',{}).get(source+':step:'+str(n),version),{**meta,**occurrence,'entity_kind':'step_evidence','step_index':n},run,
                {'chunks':chunk_fields([prefix+' / Agent动作步骤 '+str(n),
                    *slot_text,'日志关联：'+step['log_association'],*relevant],'step-'+str(n)), 'media':media})

def import_report(kb,report,downloads,raw,checkpoint=None,workers=4,authorization_ref='knowledge-base-user-app-report-import',auth_policy=None,auth_context=None):
    if auth_context:
        auth_policy.check_context(auth_context)
        from authorization import report_project,Forbidden
        stored,exists=report_project(kb,report['batch_id'])
        if exists:
            auth_policy.allow(auth_context['credential_id'],'app','update',[stored])
            if stored!=auth_context['projects'][0]:raise Forbidden('report_project_immutable')
        else:auth_policy.allow(auth_context['credential_id'],'app','create',auth_context['projects'])
    links=report.get('storage_mode')=='links_only'
    if not links:raise ValueError('App report imports require links_only storage')
    if hashlib.sha256(raw).hexdigest()!=report['html_sha256']:raise ValueError('HTML mismatch')
    input_counts=copy.deepcopy(report['counts'])
    report=prepare_report(kb,report,raw)
    # Chunk text is unchanged: reuse the established parser/model text cache profile.
    cache_report={k:v for k,v in report.items() if k!='storage_mode'}
    kb.embedding=CacheEmbedding(kb,report_profile(cache_report if links else report))
    html_ref=kb.objects.put(raw);manifest_ref=kb.objects.put(json.dumps(report,ensure_ascii=False,sort_keys=True).encode())
    version=report_version(report)
    with kb.db() as c:
        c.execute('INSERT OR IGNORE INTO app_report_inputs VALUES(?,?,?)',(report['batch_id'],report['html_sha256'],html_ref))
        if not links:
            for u,d in downloads.items():c.execute('INSERT OR REPLACE INTO app_downloads VALUES(?,?,?,?,?)',(u,d.get('object_ref'),d.get('mime'),d['status'],d.get('error')))
        c.execute('INSERT OR IGNORE INTO app_reports VALUES(?,?,?,?,?)',(report['batch_id'],version,manifest_ref,html_ref,'building'))
    total=1+2*len(report['runs'])+report['counts']['steps'];done=0;failed=[]
    def ingest(record):
        sid,v,meta,parent,doc=record
        with kb.db() as c:
            existing=c.execute('SELECT v.status FROM versions v JOIN app_entities e USING(source_id,version) WHERE v.source_id=? AND v.version=?',(sid,v)).fetchone()
        if existing and existing['status'] in {'indexed','published'}:
            return {'source_id':sid,'state':'indexed','idempotent':True}
        pref=kb.objects.put(json.dumps(parent,ensure_ascii=False,sort_keys=True).encode())
        with kb.db() as c:c.execute('INSERT OR REPLACE INTO app_entities VALUES(?,?,?,?)',(sid,v,json.dumps(meta,ensure_ascii=False),pref))
        for chunk in doc['chunks']:
            if meta['entity_kind']!='batch_summary':
                chunk['text']=meta['case_id']+' / '+meta['run_id']+' / '+meta['entity_kind']+' / '+str(meta.get('step_index',''))+'\n'+chunk['text']
        digest=hashlib.sha256(json.dumps(doc,ensure_ascii=False,sort_keys=True).encode()).hexdigest();now=time.time()
        b={'source_id':sid,'version':v,'title':parent['title'],'url':report['source_url'],'document':doc,
           'proof':{'source_id':sid,'version':v,'principal':kb.collector_principal,'view_allowed':True,'copy_allowed':True,
                    'content_hash':digest,'verified_at':now,'expires_at':now+900,'authorization_ref':authorization_ref}}
        try:
            result=kb.ingest(b)
            if not links:
                kb.publish({'source_id':sid,'version':v,'content_hash':digest,'audience':'app','approved_by':authorization_ref,'approval_ref':authorization_ref})
            return {'source_id':sid,'state':'indexed' if links else 'published','idempotent':result['idempotent']}
        except Exception as exc:return {'source_id':sid,'state':'failed',**failure(exc,'index_record',source_id=sid)}
    checkpoint=Path(checkpoint or kb.root/'import-latest.json')
    result={'batch_id':report['batch_id'],'total':total,'results':[],'started_at':time.time()}
    # Consume bounded iterator: no 1426 Base64 images held in memory simultaneously.
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        iterator=iter(records(report,downloads,kb.objects.root));pending=set()
        for _ in range(workers):
            try:pending.add(pool.submit(ingest,next(iterator)))
            except StopIteration:pass
        while pending:
            ready,pending=concurrent.futures.wait(pending,return_when=concurrent.futures.FIRST_COMPLETED)
            for f in ready:
                output=f.result();done+=1;result['results'].append(output)
                if output['state']=='failed':failed.append(output)
                if done%10==0 or done==total:
                    result.update(done=done,failures=len(failed));checkpoint.write_text(json.dumps(result))
                    print(json.dumps({'done':done,'total':total,'failures':len(failed)}),flush=True)
                try:pending.add(pool.submit(ingest,next(iterator)))
                except StopIteration:pass
    if links and not failed:
        kb.activate_report(report,authorization_ref,manifest_ref=manifest_ref,html_ref=html_ref,auth_policy=auth_policy,auth_context=auth_context)
    else:
        with kb.db() as c:c.execute('UPDATE app_reports SET status=? WHERE batch_id=? AND version=?',('published' if not failed else 'partial',report['batch_id'],version))
    result.update(finished_at=time.time(),done=done,failures=len(failed),input_coverage=input_counts,
                  coverage=report['counts'],model_calls=kb.embedding.model_calls,
                  idempotent=all(r.get('idempotent') for r in result['results']),coverage_mismatches=report['coverage_mismatches'])
    checkpoint.write_text(json.dumps(result))
    print(json.dumps({'state':'published' if not failed else 'partial','done':done,'failures':len(failed)}),flush=True)

    return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument('manifest');parser.add_argument('downloads');parser.add_argument('--workers',type=int,default=4);args=parser.parse_args()
    report=json.loads(Path(args.manifest).read_text());downloads=json.loads(Path(args.downloads).read_text())
    raw=Path('/var/lib/ai-knowledge-base/inbox/app/report.html').read_bytes()
    import_report(knowledge(settings(),'app'),report,downloads,raw,workers=args.workers)

if __name__=='__main__':main()
