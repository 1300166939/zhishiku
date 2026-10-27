"""Persistent lexical App retrieval with bounded candidate/hit reads."""
import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from retrieval import terms,rrf,bm25
from diagnostics import event,failure
from rerank import candidate_limit,rank

def tokens(text):
    # Hex encoding makes existing Chinese chars/bigrams and English identifiers exact FTS tokens.
    return ' '.join('t'+word.encode().hex() for word in terms(text))

def initialize(c,entity_table='app_entities'):
    c.create_function('app_lex_tokens',1,tokens,deterministic=True)
    c.executescript('''
    CREATE VIRTUAL TABLE IF NOT EXISTS app_lex_words USING fts5(tokens);
    CREATE VIRTUAL TABLE IF NOT EXISTS app_lex_substrings USING fts5(text,tokenize='trigram');
    CREATE TRIGGER IF NOT EXISTS app_lex_insert AFTER INSERT ON chunks BEGIN
      INSERT OR REPLACE INTO app_lex_words(rowid,tokens) VALUES(new.rowid,app_lex_tokens(new.text));
      INSERT OR REPLACE INTO app_lex_substrings(rowid,text) VALUES(new.rowid,new.text);
    END;
    CREATE TRIGGER IF NOT EXISTS app_lex_delete AFTER DELETE ON chunks BEGIN
      DELETE FROM app_lex_words WHERE rowid=old.rowid;
      DELETE FROM app_lex_substrings WHERE rowid=old.rowid;
    END;
    CREATE TRIGGER IF NOT EXISTS app_lex_update AFTER UPDATE OF text ON chunks BEGIN
      DELETE FROM app_lex_words WHERE rowid=old.rowid;
      DELETE FROM app_lex_substrings WHERE rowid=old.rowid;
      INSERT INTO app_lex_words(rowid,tokens) VALUES(new.rowid,app_lex_tokens(new.text));
      INSERT INTO app_lex_substrings(rowid,text) VALUES(new.rowid,new.text);
    END;
    CREATE TABLE IF NOT EXISTS app_search_config(key TEXT PRIMARY KEY,value TEXT);
    INSERT OR IGNORE INTO app_search_config VALUES('lexical_ready','0');
    ''')
    if entity_table=='app_entities':
        c.executescript('''CREATE INDEX IF NOT EXISTS app_entity_batch_kind ON app_entities(json_extract(metadata,'$.batch_id'),json_extract(metadata,'$.entity_kind'));
        CREATE INDEX IF NOT EXISTS app_entity_run_kind ON app_entities(json_extract(metadata,'$.run_id'),json_extract(metadata,'$.entity_kind'));''')
    if not c.execute('SELECT 1 FROM chunks LIMIT 1').fetchone():
        c.execute("UPDATE app_search_config SET value='1' WHERE key='lexical_ready'")

def backfill(kb,batch_size=250):
    count=0;cursor=0
    with kb.db() as c:c.execute("UPDATE app_search_config SET value='0' WHERE key='lexical_ready'")
    while True:
        with kb.db() as c:
            rows=c.execute('''SELECT c.rowid,c.text FROM chunks c
                LEFT JOIN app_lex_words w ON w.rowid=c.rowid
                LEFT JOIN app_lex_substrings t ON t.rowid=c.rowid
                WHERE (w.rowid IS NULL OR t.rowid IS NULL) AND c.rowid>? ORDER BY c.rowid LIMIT ?''',(cursor,batch_size)).fetchall()
            for row in rows:
                c.execute('INSERT OR REPLACE INTO app_lex_words(rowid,tokens) VALUES(?,?)',(row[0],tokens(row[1])))
                c.execute('INSERT OR REPLACE INTO app_lex_substrings(rowid,text) VALUES(?,?)',(row[0],row[1]))
            count+=len(rows)
            if rows:cursor=rows[-1][0]
        if not rows:break
        if count%10000<batch_size:event('app_lex_backfill',processed=count)
    with kb.db() as c:
        missing=c.execute('SELECT count(*) FROM chunks c LEFT JOIN app_lex_words w ON w.rowid=c.rowid LEFT JOIN app_lex_substrings t ON t.rowid=c.rowid WHERE w.rowid IS NULL OR t.rowid IS NULL').fetchone()[0]
        if missing:raise RuntimeError('Lexical index coverage incomplete')
        c.execute("UPDATE app_search_config SET value='1' WHERE key='lexical_ready'")
        total=c.execute('SELECT count(*) FROM chunks').fetchone()[0]
    return {'processed':count,'total':total,'missing':missing}

def project_field(alias='e',library='app'):
    if library=='test_api':return "json_extract("+alias+".metadata,'$.project')"
    return "CASE WHEN coalesce(json_extract("+alias+".metadata,'$.doc_type'),'app_execution_report_chunk')='app_execution_report_chunk' THEN coalesce((SELECT o.project FROM report_ownership o WHERE o.batch_id=json_extract("+alias+".metadata,'$.batch_id')),json_extract("+alias+".metadata,'$.project')) ELSE json_extract("+alias+".metadata,'$.project') END"

def filtered_sql(filters,alias='e',array_filters=False):
    clauses=[];params=[]
    for key,val in filters.items():
        field="coalesce(json_extract("+alias+".metadata,'$.doc_type'),'app_execution_report_chunk')" if key=='doc_type' else "json_extract("+alias+".metadata,'$."+key+"')"
        if key=='project':field=project_field(alias,'test_api' if array_filters else 'app')
        values=val if isinstance(val,list) else [val]
        placeholders=','.join('?' for _ in values)
        if array_filters:
            clauses.append('('+field+' IN ('+placeholders+') OR (json_type('+alias+".metadata,'$."+key+"')='array' AND EXISTS(SELECT 1 FROM json_each("+alias+".metadata,'$."+key+"') j WHERE j.value IN ("+placeholders+'))))');params.extend(values);params.extend(values)
        else:
            clauses.append(field+' IN ('+placeholders+')');params.extend(values)
    return (' AND '+' AND '.join(clauses) if clauses else ''),params

def scope_sql(kb,alias='e',filters=None):
    extra,params=filtered_sql(kb.filters if filters is None else filters,alias,array_filters=kb.library=='test_api')
    projects=getattr(kb,'auth_projects',None)
    if projects is not None:
        extra+=' AND '+project_field(alias,kb.library)+' IN ('+','.join('?' for _ in projects)+')' if projects else ' AND 0'
        params.extend(projects)
    return ''' JOIN sources s ON s.source_id='''+alias+'''.source_id AND s.active_version='''+alias+'''.version
     JOIN versions v ON v.source_id=s.source_id AND v.version=s.active_version
     JOIN publications p ON p.source_id=v.source_id AND p.version=v.version
     WHERE s.tombstone=0 AND v.status IN ('indexed','published') AND p.state='published'
     AND p.audience='''+"'"+kb.library+"'"+''' AND p.content_hash=v.content_hash'''+extra,params

@contextmanager
def stage(trace,name):
    started=time.monotonic()
    try:yield
    except Exception as exc:
        failure(exc,name,trace_id=trace);raise
    finally:event('search_stage',trace_id=trace,stage=name,elapsed_ms=round((time.monotonic()-started)*1000))

def query(kb,query,principal,top_k):
    if not isinstance(query,str) or not query.strip() or len(query)>2000:raise ValueError('Query must contain 1 to 2000 characters')
    if type(top_k) is not int or not 1<=top_k<=getattr(kb,'max_top_k',8):raise ValueError('Invalid top_k')
    trace=str(uuid.uuid4());empty={'evidence':[],'image_evidence':[],'warnings':[],'retrieval_trace_id':trace,'abstain_reason':'no_current_authorized_sources'}
    if principal!=kb.collector_principal:return empty
    scope,params=scope_sql(kb);entity_table=getattr(kb,'entity_table','app_entities');channel_limit=max(30,top_k)
    warnings=[];channels={};by_id={};metadata={}
    with stage(trace,'lexical'),kb.db() as c:
        if not c.execute('SELECT 1 FROM '+entity_table+' e'+scope+' LIMIT 1',params).fetchone():return empty
        ready=c.execute("SELECT value FROM app_search_config WHERE key='lexical_ready'").fetchone()[0]=='1'
        if not ready:
            warnings.append('lexical_index_building')
        else:
            # Small filtered scopes are bounded; avoid searching global postings once per row.
            small=c.execute('SELECT c.chunk_id,c.text FROM chunks c JOIN '+entity_table+' e ON e.source_id=c.source_id AND e.version=c.version'+scope+' LIMIT 501',params).fetchall() if kb.filters else []
            small_scope=bool(small) and len(small)<=500
            if small_scope:
                channels['bm25']=[cid for cid,_ in bm25(query,[{'chunk_id':r[0],'text':r[1]} for r in small],channel_limit)]
                channels['exact']=[r[0] for r in small if query.lower() in r[1].lower()][:channel_limit]
            match=' OR '.join('"'+word+'"' for word in sorted(set(tokens(query).split())))
            if match and not small_scope:
                rows=c.execute('''SELECT c.chunk_id FROM app_lex_words w JOIN chunks c ON c.rowid=w.rowid
                    JOIN '''+entity_table+''' e ON e.source_id=c.source_id AND e.version=c.version'''+scope+
                    ' AND app_lex_words MATCH ? ORDER BY bm25(app_lex_words),c.rowid LIMIT ?',params+[match,channel_limit]).fetchall()
                channels['bm25']=[r[0] for r in rows]
            # Trigram accelerates >=3 chars; instr preserves arbitrary substring matching.
            join=' JOIN app_lex_substrings t ON t.rowid=c.rowid' if len(query)>=3 else ''
            exact=' AND app_lex_substrings MATCH ?' if len(query)>=3 else ''
            bind=params+(['"'+query.replace('"','""')+'"'] if len(query)>=3 else [])+[query.lower()]
            if not small_scope:
                rows=c.execute('SELECT c.chunk_id FROM chunks c'+join+' JOIN '+entity_table+' e ON e.source_id=c.source_id AND e.version=c.version'+scope+
                               exact+' AND instr(lower(c.text),?)>0 ORDER BY c.rowid LIMIT ?',bind+[channel_limit]).fetchall()
                channels['exact']=[r[0] for r in rows]
    try:
        with stage(trace,'embedding'):vector=kb.embedding.embed_text(query).vector
        with stage(trace,'dense'):
            # SQLite temp IDs mean versions stream into Qdrant, without Python retaining text/full metadata.
            with kb.db() as c:
                if hasattr(kb.qdrant,'search_ids'):
                    ids=c.execute('SELECT c.chunk_id FROM chunks c JOIN '+entity_table+' e ON e.source_id=c.source_id AND e.version=c.version'+scope,params)
                    channels['dense']=kb.qdrant.search_ids(vector,ids)
                else:
                    versions=c.execute('SELECT e.source_id,e.version FROM '+entity_table+' e'+scope,params)
                    channels['dense']=kb.qdrant.search(vector,[(r[0],r[1]) for r in versions])
    except Exception:
        warnings.append('dense_unavailable_using_lexical')
    client=getattr(kb,'reranker',None)
    candidates=rrf(channels,max(top_k,candidate_limit(client,top_k)));ids=[cid for cid,_,_ in candidates]
    with stage(trace,'current_hits'),kb.db() as c:
        if ids:
            rows=c.execute('''SELECT c.*,v.title,v.url,'''+project_field('e',kb.library)+''' AS rerank_project FROM chunks c JOIN '''+entity_table+''' e
                ON e.source_id=c.source_id AND e.version=c.version'''+scope+
                ' AND c.chunk_id IN ('+','.join('?' for _ in ids)+')',params+ids).fetchall()
            by_id={r['chunk_id']:dict(r) for r in rows}
    # Current publication/filter check happens before any candidate reaches the model.
    candidates=[c for c in candidates if c[0] in by_id]
    gate=getattr(kb,'rerank_project_gate',lambda project:True)
    eligible={cid:r['text'] for cid,r in by_id.items() if gate(r.get('rerank_project'))}
    if not eligible:client=None
    with stage(trace,'rerank'):
        rerank_count=client.candidates if client and client.enabled else len(candidates)
        head,tail=candidates[:rerank_count],candidates[rerank_count:]
        head,rerank_scores,ranking,rerank_warnings=rank(client,query,head,
            eligible,min(top_k,len(head)),trace)
        ranking['library_disabled_count']=len(by_id)-len(eligible)
        candidates=(head+tail)[:top_k]
    warnings.extend(rerank_warnings)
    budget=getattr(kb,'result_char_budget',12000);evidence=[]
    for cid,score,channel in candidates:
        row=by_id.get(cid)
        if not row or len(row['text'])>budget:continue
        budget-=len(row['text'])
        evidence.append({'evidence_id':cid,'kind':'text','text':row['text'],'source_id':row['source_id'],'source_version':row['version'],
            'title':row['title'],'source_url':row['url']+('#'+row['block_id'] if row['block_id'] else ''),
            'heading_path':row['heading_path'],'score':score,'channels':channel,'trust_status':'source_document','permission_mode':'app_library_publication'})
        if cid in rerank_scores:evidence[-1]['rerank_score']=rerank_scores[cid]
    result={'evidence':evidence,'image_evidence':[],'warnings':warnings,'retrieval_trace_id':trace,'abstain_reason':None if evidence else 'no_authorized_evidence'}
    result['ranking']=ranking
    with stage(trace,'enrich'):result=kb.enrich(result)
    if not result['evidence']:result['abstain_reason']='no_authorized_evidence'
    event('search_complete',trace_id=trace,hits=len(result['evidence']),candidates=len(ids),warnings=warnings)
    return result
