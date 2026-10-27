"""API knowledge: original typed metadata, bounded retrieval and atomic text upserts."""
import hashlib,json,math,re,time,uuid
from contextlib import contextmanager,nullcontext
from core import Knowledge
from app_search import initialize,scope_sql,tokens
DOC_TYPES={'testcase_chunk','dependency_rule_chunk','failure_observation_chunk','failure_fix_chunk','testsuit_chunk','environment_config_chunk','api_success_pattern_chunk','journey_success_pattern_chunk','historical_execution_success_chunk'}
FILTERS={'kb_domain','doc_type','project','module','env','api_path','source_table','source_id','source_hash','verified','confidence','case_num','method','suite_name','suitname','status','knowledge_fingerprint','canonical_id','service','department','verification_mode','schema_version','review_status'}
PROFILE='api-source-text-v1:max6000:ark2048'

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
def validate_filters(filters):
 if filters is None:return {}
 if not isinstance(filters,dict) or any(not isinstance(k,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}',k) for k in filters):raise ValueError('Invalid API filters')
 result={}
 for key,value in filters.items():
  if value in (None,'',[]):continue
  values=value if isinstance(value,list) else [value]
  if len(values)>50 or any(type(v) not in (str,int,float,bool) for v in values):raise ValueError('Invalid filter values')
  if key=='verified' and any(type(v) is not bool for v in values):raise ValueError('verified must be boolean')
  if key=='kb_domain' and values!=['test_api']:raise ValueError('Only test_api allowed')
  result[key]=value
 return result

def validate_items(body):
 if not isinstance(body,dict) or set(body)-{'items','request_id'}:raise ValueError('Invalid API import body')
 items=body.get('items')
 if not isinstance(items,list) or not 1<=len(items)<=100:raise ValueError('Expected 1 to 100 items')
 seen=set();out=[]
 for item in items:
  if not isinstance(item,dict) or set(item)-{'id','text','metadata'}:raise ValueError('Invalid item')
  iid=item.get('id');text=item.get('text');meta=item.get('metadata',{})
  if not isinstance(iid,str) or not iid or len(iid)>256 or iid in seen:raise ValueError('Invalid or duplicate id')
  if not isinstance(text,str) or not text.strip() or len(text)>200000:raise ValueError('Invalid API text')
  if not isinstance(meta,dict) or meta.get('kb_domain','test_api')!='test_api' or meta.get('doc_type') not in DOC_TYPES:raise ValueError('Invalid API domain/type')
  # No defaults for verified/review/confidence/project; preserve source knowledge assertions.
  if 'verified' in meta and type(meta['verified']) is not bool:raise ValueError('Invalid verified')
  if 'confidence' in meta and (type(meta['confidence']) not in (int,float) or not math.isfinite(meta['confidence'])):raise ValueError('Invalid confidence')
  meta={**meta,'kb_domain':'test_api'};json.dumps(meta,allow_nan=False)
  seen.add(iid);out.append({'id':iid,'text':text,'metadata':meta})
 return out

def parts(text):
 if len(text)<=6000:return [{'text':text,'block_id':'part-0','heading_path':''}]
 # Existing API chunk is retained in full; long payloads split only at line boundaries.
 out=[];buf=''
 for line in text.splitlines(keepends=True):
  for offset in range(0,len(line),5800):
   piece=line[offset:offset+5800]
   if buf and len(buf)+len(piece)>6000:out.append({'text':buf,'block_id':'part-'+str(len(out)),'heading_path':''});buf=''
   buf+=piece
 if buf:out.append({'text':buf,'block_id':'part-'+str(len(out)),'heading_path':''})
 return out

class ApiKnowledge(Knowledge):
 entity_table='api_entities';max_top_k=50;result_char_budget=300000
 def __init__(self,*args,filters=None,**kwargs):
  super().__init__(*args,library='test_api',**kwargs);self.filters=validate_filters(filters)
  with self.db() as c:
   c.execute('CREATE TABLE IF NOT EXISTS api_entities(source_id TEXT,version TEXT,item_id TEXT,metadata TEXT,item_hash TEXT,PRIMARY KEY(source_id,version))')
   for field in ['project','doc_type','env','module','api_path','source_id','source_table','verified']:
    c.execute("CREATE INDEX IF NOT EXISTS api_meta_"+field+" ON api_entities(json_extract(metadata,'$."+field+"'))")
   initialize(c,entity_table='api_entities')
 @contextmanager
 def db(self):
  with super().db() as c:
   c.create_function('app_lex_tokens',1,tokens,deterministic=True);c.execute('PRAGMA recursive_triggers=ON');yield c
 def authorized(self,principal):
  if principal!=self.collector_principal:return []
  scope,params=scope_sql(self)
  with self.db() as c:return [dict(r) for r in c.execute('SELECT v.* FROM api_entities e'+scope,params)]
 def enrich(self,result):
  scope,params=scope_sql(self);evidence=result.get('evidence',[]);keys={(ev['source_id'],ev['source_version']) for ev in evidence}
  if not keys:return result
  clause=' AND ('+' OR '.join('(e.source_id=? AND e.version=?)' for _ in keys)+')';bind=params+[v for key in keys for v in key]
  with self.db() as c:entities={(r['source_id'],r['version']):dict(r) for r in c.execute('SELECT e.* FROM api_entities e'+scope+clause,bind)}
  out=[]
  for ev in evidence:
   e=entities.get((ev['source_id'],ev['source_version']))
   if not e:continue
   meta=json.loads(e['metadata']);ev.update(metadata=meta,original_id=e['item_id'],library='test_api',permission_mode='api_library_publication',trust_status='imported_api_knowledge')
   ev['source_locator']={k:meta[k] for k in ['source_table','source_id','source_hash','api_path','method'] if k in meta};out.append(ev)
  result['evidence']=out;result['library']='test_api';return result
 def query(self,query,principal,top_k=5):
  from app_search import query as optimized_query
  return optimized_query(self,query,principal,top_k)
 def upsert(self,items,approval='api-maintainer',approval_ref='authorized-api-import',commit_guard=None):
  from concurrent.futures import ThreadPoolExecutor
  # A bounded batch warms unique vectors in parallel; every source still publishes atomically.
  with self.db() as c:c.execute('CREATE TABLE IF NOT EXISTS api_vector_cache(cache_key TEXT PRIMARY KEY,object_ref TEXT)')
  work={digest([PROFILE,ch['text']]):ch['text'] for item in items for ch in parts(item['text'])}
  def warm(entry):
   key,text=entry
   with self.db() as c:cached=c.execute('SELECT object_ref FROM api_vector_cache WHERE cache_key=?',(key,)).fetchone()
   if cached:return
   for attempt in range(3):
    try:vector=self.embedding.embed_text(text).vector;break
    except Exception:
     if attempt==2:raise
     time.sleep(2**attempt)
   ref=self.objects.put(json.dumps(vector).encode())
   with self.db() as c:c.execute('INSERT OR IGNORE INTO api_vector_cache VALUES(?,?)',(key,ref))
  with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(warm,work.items()))
  # Compute outside a write transaction; an exception leaves every active source intact.
  staged=[];unchanged=0;pending_vectors=[]
  for item in items:
   sid='test_api:item:'+digest(item['id']);h=digest(item);version='api-v1-'+h[:24]
   with self.db() as c:
    old=c.execute('SELECT s.active_version,e.item_hash FROM sources s JOIN api_entities e ON e.source_id=s.source_id AND e.version=s.active_version WHERE s.source_id=?',(sid,)).fetchone()
   if old and old['item_hash']==h:
    unchanged+=1;continue
   doc={'chunks':parts(item['text']),'media':[],'original_item':item,'profile':PROFILE};raw=json.dumps(doc,ensure_ascii=False,sort_keys=True).encode();dh=hashlib.sha256(raw).hexdigest()
   chunk_points=[]
   for i,chunk in enumerate(doc['chunks']):
    cid=str(uuid.uuid5(uuid.NAMESPACE_URL,sid+'/'+version+'/'+str(i)+'/'+dh));key=digest([PROFILE,chunk['text']])
    with self.db() as c:
     c.execute('CREATE TABLE IF NOT EXISTS api_vector_cache(cache_key TEXT PRIMARY KEY,object_ref TEXT)')
     cache=c.execute('SELECT object_ref FROM api_vector_cache WHERE cache_key=?',(key,)).fetchone()
    if cache:vector=json.loads(self.objects.get(cache[0]))
    else:
     result=self.embedding.embed_text(chunk['text']);vector=result.vector
     ref=self.objects.put(json.dumps(vector).encode())
     with self.db() as c:c.execute('INSERT OR IGNORE INTO api_vector_cache VALUES(?,?)',(key,ref))
    if hasattr(self.qdrant,'upsert_many'):
     pending_vectors.append({'id':cid,'vector':vector,'payload':{'source_id':sid,'version':version}})
    else:
     self.qdrant.upsert(cid,vector,sid,version)
     if not self.qdrant.exists(cid):raise RuntimeError('API vector reconciliation failed')
    chunk_points.append((cid,sid,version,chunk['text'],chunk['block_id'],chunk['heading_path']))
   object_ref=self.objects.put(raw)
   staged.append((item,sid,version,h,dh,object_ref,chunk_points))
  for offset in range(0,len(pending_vectors),100):
   batch=pending_vectors[offset:offset+100];self.qdrant.upsert_many(batch)
   if self.qdrant.exists_many([p['id'] for p in batch])!={p['id'] for p in batch}:raise RuntimeError('API vector reconciliation failed')
  with (commit_guard() if commit_guard else nullcontext()),self.db() as c:
   c.execute('BEGIN IMMEDIATE')
   for item,sid,version,h,dh,ref,chunk_points in staged:
    meta=item['metadata'];c.execute('INSERT OR IGNORE INTO sources(source_id) VALUES(?)',(sid,))
    if c.execute('SELECT tombstone FROM sources WHERE source_id=?',(sid,)).fetchone()[0]:raise ValueError('Revoked API source')
    c.execute('INSERT OR REPLACE INTO versions VALUES(?,?,?,?,?,?,?,?)',(sid,version,str(meta.get('title',item['id'])),str(meta.get('source_url','')),ref,dh,json.dumps({'mode':'authorized_api_transfer','approved_by':approval,'approval_ref':approval_ref}),'indexed'))
    c.executemany('INSERT OR REPLACE INTO chunks VALUES(?,?,?,?,?,?)',chunk_points)
    c.execute('INSERT OR REPLACE INTO api_entities VALUES(?,?,?,?,?)',(sid,version,item['id'],json.dumps(meta,ensure_ascii=False,sort_keys=True),h))
    c.execute('INSERT OR REPLACE INTO publications VALUES(?,?,?,?,?,?,?,?,NULL)',(sid,version,dh,'test_api',approval,approval_ref,time.time(),'published'))
    c.execute('UPDATE sources SET active_version=? WHERE source_id=?',(version,sid))
  return {'ok':True,'count':len(items),'changed':len(staged),'unchanged':unchanged,'kb_domain':'test_api'}
 def item(self,iid):
  sid='test_api:item:'+digest(iid)
  with self.db() as c:r=c.execute('SELECT v.object_ref FROM versions v JOIN sources s USING(source_id) WHERE v.source_id=? AND v.version=s.active_version AND s.tombstone=0',(sid,)).fetchone()
  return json.loads(self.objects.get(r[0]))['original_item'] if r else None
