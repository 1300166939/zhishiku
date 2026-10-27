from contextlib import nullcontext
"""App-specialized Registry, exact filters, parent evidence and storage provenance."""
import hashlib
import json
import time
from contextlib import contextmanager
from core import Knowledge

MAP_TYPE='app_page_rule_chunk'
REPORT_TYPE='app_execution_report_chunk'
FILTERS={'project','doc_type','page_id','batch_id','case_id','run_id','platform','reported_status','reported_reason','review_status','app_build','environment','entity_kind','step_index'}

def matches(meta,filters):
    return all(meta.get(k,REPORT_TYPE if k=='doc_type' else None) in val
               if isinstance(val,list) else meta.get(k,REPORT_TYPE if k=='doc_type' else None)==val
               for k,val in filters.items())

class AppKnowledge(Knowledge):
    def __init__(self,*args,filters=None,**kwargs):
        super().__init__(*args,library='app',**kwargs)
        self.filters=validate_filters(filters)
        with self.db() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS app_reports(batch_id TEXT, version TEXT,
                manifest_ref TEXT, html_ref TEXT, status TEXT, PRIMARY KEY(batch_id,version));
                CREATE TABLE IF NOT EXISTS app_entities(source_id TEXT, version TEXT, metadata TEXT,
                parent_ref TEXT, PRIMARY KEY(source_id,version));
                CREATE TABLE IF NOT EXISTS app_downloads(url TEXT PRIMARY KEY, object_ref TEXT, mime TEXT,
                status TEXT, error TEXT);
                CREATE TABLE IF NOT EXISTS app_report_inputs(batch_id TEXT, html_sha256 TEXT, html_ref TEXT,
                PRIMARY KEY(batch_id,html_sha256));''')
            c.execute("CREATE INDEX IF NOT EXISTS app_project_type ON app_entities(json_extract(metadata,'$.project'),coalesce(json_extract(metadata,'$.doc_type'),'app_execution_report_chunk'))")
            c.execute("CREATE TABLE IF NOT EXISTS report_ownership(batch_id TEXT PRIMARY KEY,project TEXT,credential_id TEXT)")
            from app_search import initialize
            initialize(c)

    @contextmanager
    def db(self):
        with super().db() as c:
            from app_search import tokens
            c.create_function('app_lex_tokens',1,tokens,deterministic=True)
            c.execute('PRAGMA recursive_triggers=ON')
            yield c

    def activate_report(self,report,approval,manifest_ref=None,html_ref=None,auth_policy=None,auth_context=None):
        """Validate every indexed document, then publish an entire report atomically."""
        from import_app import report_version
        version=report_version(report);batch=report['batch_id']
        expected=1+2*len(report['runs'])+report['counts']['steps']
        entity_versions=report.get('entity_versions')
        with self.db() as c:
            if entity_versions:
                rows=[]
                for sid,v in entity_versions.items():
                    row=c.execute('SELECT v.*,s.tombstone,e.metadata FROM versions v JOIN sources s USING(source_id) JOIN app_entities e USING(source_id,version) WHERE v.source_id=? AND v.version=?',(sid,v)).fetchone()
                    if row and json.loads(row['metadata']).get('batch_id')==batch:rows.append(dict(row))
            else:
                rows=[dict(r) for r in c.execute('SELECT v.*,s.tombstone,e.metadata FROM versions v JOIN sources s USING(source_id) JOIN app_entities e USING(source_id,version) WHERE v.version=?',(version,))
                      if json.loads(r['metadata']).get('batch_id')==batch]
        if len(rows)!=expected:raise ValueError('Incomplete report')
        for row in rows:
            v=row['version']
            if row['tombstone'] or row['status'] not in {'indexed','published'}:raise ValueError('Unreconciled report source')
            raw=self.objects.get(row['object_ref']);doc=json.loads(raw)
            if hashlib.sha256(raw).hexdigest()!=row['content_hash'] or doc.get('media'):raise ValueError('Invalid links-only snapshot')
            with self.db() as c:
                chunks=list(c.execute('SELECT chunk_id FROM chunks WHERE source_id=? AND version=?',(row['source_id'],v)))
                if len(chunks)!=len(doc['chunks']) or c.execute('SELECT count(*) FROM assets WHERE source_id=? AND version=?',(row['source_id'],v)).fetchone()[0]:raise ValueError('Incomplete links-only index')
            if not all(self.qdrant.exists(r[0]) for r in chunks):raise ValueError('Missing text vector')
        with (auth_policy.commit([auth_context]) if auth_context else nullcontext()),self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if auth_context:
                from authorization import Forbidden
                old=c.execute('SELECT project FROM report_ownership WHERE batch_id=?',(batch,)).fetchone()
                existed=c.execute("SELECT 1 FROM app_reports WHERE batch_id=? AND status='published'",(batch,)).fetchone()
                stored=old[0] if old else None
                if old or existed:
                    auth_policy.allow(auth_context['credential_id'],'app','update',[stored])
                    if stored!=auth_context['projects'][0]:raise Forbidden('report_project_immutable')
                else:auth_policy.allow(auth_context['credential_id'],'app','create',auth_context['projects'])
                c.execute('INSERT OR IGNORE INTO report_ownership VALUES(?,?,?)',(batch,auth_context['projects'][0],auth_context['credential_id']))
            if entity_versions:
                previous=c.execute("SELECT version FROM app_reports WHERE batch_id=? AND status='published'",(batch,)).fetchone()
                if (previous['version'] if previous else None)!=report['previous_publication']:
                    raise ValueError('Concurrent report publication changed')
            for row in rows:
                v=row['version']
                current=c.execute('SELECT v.content_hash,v.status,s.tombstone FROM versions v JOIN sources s USING(source_id) WHERE v.source_id=? AND v.version=?',(row['source_id'],v)).fetchone()
                if current['tombstone'] or current['content_hash']!=row['content_hash'] or current['status'] not in {'indexed','published'}:raise ValueError('Report changed during activation')
                c.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,NULL) ON CONFLICT(source_id,version) DO UPDATE SET state=excluded.state,published_at=excluded.published_at,withdrawn_at=NULL',
                    (row['source_id'],v,row['content_hash'],'app',approval,approval,time.time(),'published'))
                c.execute('UPDATE sources SET active_version=? WHERE source_id=?',(v,row['source_id']))
            if entity_versions:
                # Changed runs may have fewer steps; withdraw obsolete steps without tombstoning history.
                stale=[r['source_id'] for r in c.execute("SELECT e.source_id FROM app_entities e JOIN sources s USING(source_id) WHERE s.active_version=e.version AND json_extract(e.metadata,'$.batch_id')=?",(batch,))
                       if r['source_id'] not in entity_versions]
                for sid in stale:
                    c.execute("UPDATE publications SET state='withdrawn',withdrawn_at=? WHERE source_id=?",(time.time(),sid))
                    c.execute('UPDATE sources SET active_version=NULL WHERE source_id=?',(sid,))
            c.execute("UPDATE app_reports SET status='superseded' WHERE batch_id=? AND version<>?",(batch,version))
            c.execute("UPDATE app_reports SET status='published' WHERE batch_id=? AND version=?",(batch,version))
            if manifest_ref and html_ref:
                c.execute('UPDATE app_reports SET manifest_ref=?,html_ref=? WHERE batch_id=? AND version=?',(manifest_ref,html_ref,batch,version))

    def additional_object_reference(self,c,ref):
        # Private immutable report manifests keep raw evidence after source withdrawal.
        found=(c.execute('SELECT 1 FROM app_downloads WHERE object_ref=?',(ref,)).fetchone()
               or c.execute('SELECT 1 FROM app_entities WHERE parent_ref=?',(ref,)).fetchone()
               or c.execute('SELECT 1 FROM app_report_inputs WHERE html_ref=?',(ref,)).fetchone()
               or c.execute('SELECT 1 FROM app_reports WHERE manifest_ref=? OR html_ref=?',(ref,ref)).fetchone())
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='app_vector_cache'").fetchone():
            found=found or c.execute('SELECT 1 FROM app_vector_cache WHERE object_ref=?',(ref,)).fetchone()
        jobdb=self.root/'import-jobs.sqlite3'
        if jobdb.exists():
            import sqlite3
            with sqlite3.connect(jobdb) as jobs:
                if jobs.execute("SELECT 1 FROM sqlite_master WHERE name='map_jobs'").fetchone():
                    found=found or jobs.execute('SELECT 1 FROM map_jobs WHERE input_ref=?',(ref,)).fetchone()
        return bool(found)

    def authorized(self,principal):
        if principal!=self.collector_principal:return []
        from app_search import scope_sql
        scope,params=scope_sql(self)
        with self.db() as c:
            return [dict(r) for r in c.execute('SELECT v.* FROM app_entities e'+scope,params)]

    def enrich(self,result):
        from app_search import scope_sql
        scope,params=scope_sql(self)
        evidence=result.get('evidence',[])+result.get('image_evidence',[])
        keys={(ev['source_id'],ev['source_version']) for ev in evidence}
        if not keys:return result
        key_sql=' AND ('+' OR '.join('(e.source_id=? AND e.version=?)' for _ in keys)+')'
        bind=params+[v for key in keys for v in key]
        with self.db() as c:
            entities={(x['source_id'],x['version']):dict(x) for x in c.execute('SELECT e.* FROM app_entities e'+scope+key_sql,bind)}
            assets=[]
        allowed=set(entities);parents={}
        for ev in result.get('evidence',[])+result.get('image_evidence',[]):
            key=(ev['source_id'],ev['source_version']);entity=entities.get(key)
            ev['library']='app';ev['permission_mode']='app_library_publication'
            if not entity or key not in allowed:continue
            meta=json.loads(entity['metadata']);meta.setdefault('doc_type',REPORT_TYPE)
            if meta['doc_type']==REPORT_TYPE:
                with self.db() as c:ownership=c.execute('SELECT project FROM report_ownership WHERE batch_id=?',(meta.get('batch_id'),)).fetchone()
                if ownership:meta['project']=ownership[0]
            ev['metadata']=meta
            if meta['doc_type']==MAP_TYPE:
                ref=entity['parent_ref']
                if ref not in parents:parents[ref]=json.loads(self.objects.get(ref))
                parent=parents[ref]
                ev['source_locator']={'page_id':meta.get('page_id'),'heading_path':ev.get('heading_path'),'item_id':meta['item_id']}
                ev['parent_context']={k:parent[k] for k in ['title','path','source_version'] if k in parent}
                ev['trust_status']='author_provided_map'
                ev['screenshots']=[{'asset_url':u,'source_image_url':u,'storage_mode':'source_url',
                    'trust_status':'source_image_link','page_id':meta.get('page_id')} for u in parent.get('image_urls',[])]
                ev['image_coverage']='links_only_not_indexed'
                continue
            ev['source_url']=ev['source_url'].split('#')[0]
            ev['source_locator']={'case_key':meta.get('case_key'),'run_id':meta.get('run_id'),'step_index':meta.get('step_index')}
            for field in ['source_step_index','attempt_index']:
                if field in meta:ev['source_locator'][field]=meta[field]
            ref=entity['parent_ref']
            if ref not in parents:parents[ref]=json.loads(self.objects.get(ref))
            parent=parents[ref]
            ev['parent_context']={k:parent[k] for k in ['case_id','run_id','title','reported_status','reported_reason','review_status','declared_steps','rendered_steps'] if k in parent}
            n=meta.get('step_index')
            if n is not None:
                steps=parent.get('steps',[]);pos=next((i for i,s in enumerate(steps) if s['step_index']==n),None)
                ev['adjacent_steps']=steps[max(0,pos-1):pos+2] if pos is not None else []
                if pos is not None and 'screenshot_slots' in steps[pos]:ev['screenshot_slots']=steps[pos]['screenshot_slots']
            if meta.get('storage_mode')=='links_only':
                ev['screenshots']=[{'asset_url':s['url'],'source_image_url':s['url'],'role':s['role'],
                    'storage_mode':'source_url','trust_status':'source_image_link',
                    'run_id':meta['run_id'],'step_index':n}
                    for s in (steps[pos]['screenshots'] if n is not None and pos is not None else [])]
                ev['image_coverage']='links_only_not_indexed'
                continue
            ev['screenshots']=[{'asset_id':a['asset_id'],'asset_url':'/v1/assets/app/'+a['asset_id'],
                               'role':a['kind'],'image_sha256':a['object_ref'],'trust_status':'source_image'}
                               for a in assets if (a['source_id'],a['version'])==key]
            if 'asset_url' in ev:ev['asset_url']='/v1/assets/app/'+ev['asset_id']
        # A withdrawal/version change during object loading must not escape in the response.
        with self.db() as c:
            allowed={(x[0],x[1]) for x in c.execute('SELECT e.source_id,e.version FROM app_entities e'+scope+key_sql,bind)}
        result['evidence']=[ev for ev in result.get('evidence',[]) if (ev['source_id'],ev['source_version']) in allowed]
        result['image_evidence']=[ev for ev in result.get('image_evidence',[]) if (ev['source_id'],ev['source_version']) in allowed]
        result['library']='app';return result

    def query(self,query,principal,top_k=5):
        from app_search import query as optimized_query
        return optimized_query(self,query,principal,top_k)
    def query_image(self,image,mime,principal,top_k=5):
        if not image or len(image)>1_000_000 or mime not in {'image/png','image/jpeg','image/webp'} or type(top_k) is not int or not 1<=top_k<=8:raise ValueError('Invalid image query')
        if principal!=self.collector_principal:return {'evidence':[],'image_evidence':[],'warnings':[],'abstain_reason':'no_current_authorized_sources'}
        from app_search import scope_sql
        scope,params=scope_sql(self)
        with self.db() as c:
            if not c.execute('SELECT 1 FROM app_entities e'+scope+' LIMIT 1',params).fetchone():return {'evidence':[],'image_evidence':[],'warnings':[],'abstain_reason':'no_current_authorized_sources'}
            has_images=c.execute('SELECT 1 FROM assets a JOIN app_entities e ON e.source_id=a.source_id AND e.version=a.version'+scope+' LIMIT 1',params).fetchone()
        if not has_images:
            return {'library':'app','evidence':[],'image_evidence':[],'warnings':[],
                    'abstain_reason':'image_search_unavailable_links_only','image_coverage':'links_only_not_indexed'}
        return self.enrich(super().query_image(image,mime,principal,top_k))

    def reports(self,filters=None):
        filters=validate_filters(filters)
        from app_search import scope_sql
        scope,params=scope_sql(self,filters={**filters,'entity_kind':'case_run_summary'})
        with self.db() as c:
            runs=[json.loads(x['metadata']) for x in c.execute('SELECT e.metadata FROM app_entities e'+scope,params)]
            if filters.get('batch_id'):
                batches=[dict(x) for x in c.execute('SELECT * FROM app_reports WHERE batch_id=?',(filters['batch_id'],))]
            else:batches=[dict(x) for x in c.execute('SELECT * FROM app_reports')]
        with self.db() as c:ownership=dict(c.execute('SELECT batch_id,project FROM report_ownership'))
        runs=[{**r,'project':ownership[r['batch_id']]} if r.get('batch_id') in ownership else r for r in runs]
        runs=[r for r in runs if matches(r,filters)]
        if getattr(self,'auth_projects',None) is not None or filters.get('project') is not None:
            allowed_batches={r['batch_id'] for r in runs}
            batches=[b for b in batches if b['batch_id'] in allowed_batches]
        if getattr(self,'auth_projects',None) is not None:
            batches=[b for b in batches if b['status']=='published']
        # Retain only summaries, not every historical manifest and its full step/log arrays.
        summaries=[]
        for b in batches:
            manifest=json.loads(self.objects.get(b['manifest_ref']))
            summaries.append({**{k:v for k,v in b.items() if not k.endswith('_ref')},
                              'storage_mode':manifest.get('storage_mode','full_images'),
                              'coverage':manifest['counts'],'coverage_mismatches':manifest['coverage_mismatches']})
            del manifest
        return {'library':'app','runs':runs,'counts':{'runs':len(runs),
                'reported_success':sum(r['reported_status']=='success' for r in runs),
                'reported_failed':sum(r['reported_status']=='failed' for r in runs),
                'confirmed_defects':sum(r['review_status']=='confirmed_defect' for r in runs)},
                'batches':summaries}

def validate_filters(filters):
    if filters is None:return {}
    if not isinstance(filters,dict) or set(filters)-FILTERS:raise ValueError('Invalid App filters')
    for key,value in filters.items():
        values=value if isinstance(value,list) else [value]
        if isinstance(value,list) and (key not in {'doc_type','project'} or not 1<=len(value)<=8):raise ValueError('Invalid list filter')
        if any(not isinstance(v,(str,int)) or isinstance(v,bool) or (isinstance(v,str) and not v) for v in values):raise ValueError('Invalid filter value')
        if key=='doc_type' and any(v not in {MAP_TYPE,REPORT_TYPE} for v in values):raise ValueError('Invalid document type')
    return filters
