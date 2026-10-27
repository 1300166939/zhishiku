"""Bounded evidence-first QA. Extracts source text; no unverifiable model claims."""
import re
import time
import uuid
from urllib.parse import unquote,urlparse
from contextlib import contextmanager
from governance import resolve
from retrieval import terms
from diagnostics import event

def tokens(text):
    # Single Chinese characters alone are too weak to support a QA answer.
    return ' '.join('t'+w.encode().hex() for w in terms(text) if len(w)>=2 and w not in {'哪些','什么','如何','是否'})

def family(row, metadata):
    if metadata.get('family'):return metadata['family']
    path=row.get('heading_path','')
    if not path:return row['source_id']
    parts=[p for p in path.split('/') if not re.fullmatch(r'\d{8}-\d{6}',p)]
    parts[-1]=re.sub(r'[_-]?[Vv]\d+(?:\.\d+)*','',parts[-1])
    return 'inferred_path:'+ '/'.join(parts)

def authorized(kb, principal):
    if kb.library=='common':return kb.authorized(principal)
    if principal!=kb.collector_principal:return []
    # Preserve private SQL scope without materializing original documents/proofs.
    from app_search import scope_sql
    clause,params=scope_sql(kb);table=getattr(kb,'entity_table','app_entities')
    with kb.db() as c:
        return [dict(r) for r in c.execute('SELECT v.source_id,v.version,\'\' AS url FROM '+table+' e'+clause,params)]

@contextmanager
def stage(trace, timings, name):
    start=time.monotonic()
    try:yield
    finally:
        timings[name]=round((time.monotonic()-start)*1000,3)
        event('qa_stage',trace_id=trace,stage=name,elapsed_ms=timings[name])

def build_index(kb):
    """Explicit admin work, transactional. Requests never silently rebuild indexes."""
    with kb.db() as c:
        c.create_function('qa_tokens',1,tokens,deterministic=True)
        c.executescript('''CREATE VIRTUAL TABLE IF NOT EXISTS qa_lex USING fts5(tokens);
        CREATE TRIGGER IF NOT EXISTS qa_insert AFTER INSERT ON chunks BEGIN
          INSERT INTO qa_lex(rowid,tokens) VALUES(new.rowid,qa_tokens(new.text)); END;
        CREATE TRIGGER IF NOT EXISTS qa_delete AFTER DELETE ON chunks BEGIN
          DELETE FROM qa_lex WHERE rowid=old.rowid; END;
        CREATE TRIGGER IF NOT EXISTS qa_update AFTER UPDATE OF text ON chunks BEGIN
          DELETE FROM qa_lex WHERE rowid=old.rowid;
          INSERT INTO qa_lex(rowid,tokens) VALUES(new.rowid,qa_tokens(new.text)); END;''')
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM qa_lex')
        c.execute('INSERT INTO qa_lex(rowid,tokens) SELECT rowid,qa_tokens(text) FROM chunks')
        count=c.execute('SELECT count(*) FROM qa_lex').fetchone()[0]
        if count!=c.execute('SELECT count(*) FROM chunks').fetchone()[0]:raise ValueError('Index count mismatch')
    return {'indexed_chunks':count}

def scope(c, current):
    c.execute('CREATE TEMP TABLE qa_allowed(source_id TEXT,version TEXT,PRIMARY KEY(source_id,version))')
    c.executemany('INSERT INTO qa_allowed VALUES(?,?)',current)

def query(kb, question, principal, top_k=5):
    if not isinstance(question,str) or not 1<=len(question.strip())<=2000 or type(top_k) is not int or not 1<=top_k<=8:
        raise ValueError('Invalid QA request')
    trace=str(uuid.uuid4());timings={};warnings=[];start=time.monotonic()
    with stage(trace,timings,'authorization_governance'):
        source_rows=authorized(kb,principal)
        current=resolve(kb,source_rows)
    if not current:return response([],[],warnings,timings,trace,'no_current_authorized_evidence',start)
    with stage(trace,timings,'lexical_candidates'),kb.db() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='qa_lex'").fetchone():
            return response([],[],['qa_index_not_built'],timings,trace,'qa_index_not_ready',start)
        scope(c,current)
        match=' OR '.join(dict.fromkeys(tokens(question).split()))
        if not match:return response([],[],warnings,timings,trace,'no_lexical_evidence',start)
        # Candidate bodies are bounded, and never include unauthorized versions.
        project_names=[]
        for r in source_rows:
            parts=unquote(urlparse(r['url']).path).split('/')
            for i,p in enumerate(parts):
                if re.fullmatch(r'v\d+',p) and i and len(parts[i-1])>=3 and parts[i-1] in question:
                    project_names.append(parts[i-1])
        named=max(project_names,key=len) if project_names else None
        extra=' AND instr(ch.heading_path,?)>0' if named else ''
        rows=[dict(r) for r in c.execute('''SELECT ch.rowid AS rowid,ch.*,v.title,v.url,bm25(qa_lex) AS lexical_score
          FROM qa_lex JOIN chunks ch ON ch.rowid=qa_lex.rowid JOIN qa_allowed a
          ON a.source_id=ch.source_id AND a.version=ch.version JOIN versions v
          ON v.source_id=ch.source_id AND v.version=ch.version
          WHERE qa_lex MATCH ?'''+extra+' ORDER BY bm25(qa_lex) LIMIT 80',[match]+([named] if named else []))]
    # If a named project appears literally, require it. Generic questions retain lexical evidence.
    names=[]
    for r in rows:
        parts=r['heading_path'].split('/')
        for i,p in enumerate(parts):
            if re.fullmatch(r'v\d+',p) and i and len(parts[i-1])>=3 and parts[i-1] in question:
                names.append(parts[i-1])
    if names:
        name=max(names,key=len);rows=[r for r in rows if name in r['heading_path'] or name in r['title']]
    elif not named:
        # A matching generic tail (e.g. 报名资格) cannot establish an unknown subject.
        runs=re.findall(r'[\u4e00-\u9fff]{6,}',question)
        if runs:
            subject=re.sub(r'^(请问|帮我|告诉我)','',runs[0])[:4]
            anchored=[r for r in rows if subject in r['text'] or subject in r['title'] or subject in r['heading_path']]
            if not anchored:return response([],[],['subject_not_supported'],timings,trace,'insufficient_subject_evidence',start)
            rows=anchored
    desired='feature' if any(w in question for w in ['用例','功能清单','核心功能']) else 'rules'
    def priority(r):
        path=r['heading_path'].lower();m=current[(r['source_id'],r['version'])]
        kind=0 if '02-feature-list' in path and desired=='feature' else (1 if '01-prd' in path else 2)
        return (m.get('state')!='approved',kind,r['lexical_score'])
    rows.sort(key=priority)
    selected=[];seen=set()
    for r in rows:
        # Reserve slots for different documents; no filename-version inference.
        key=family(r,current[(r['source_id'],r['version'])])
        if key in seen:continue
        seen.add(key);selected.append(r)
        if len(selected)>=top_k:break
    with stage(trace,timings,'related_sections'),kb.db() as c:
        scope(c,current)
        expanded=[]
        for r in selected:
            neighbors=[dict(x) for x in c.execute('''SELECT ch.*,v.title,v.url FROM chunks ch
              JOIN qa_allowed a ON a.source_id=ch.source_id AND a.version=ch.version
              JOIN versions v ON v.source_id=ch.source_id AND v.version=ch.version
              WHERE ch.source_id=? AND ch.version=? AND ch.rowid BETWEEN ? AND ? ORDER BY ch.rowid LIMIT 3''',
              (r['source_id'],r['version'],r['rowid']-1,r['rowid']+1))] if 'rowid' in r else []
            expanded.append(r);expanded.extend(x for x in neighbors if x['chunk_id']!=r['chunk_id'])
    with stage(trace,timings,'citation_assembly'):
        latest=resolve(kb,authorized(kb,principal));evidence=[];claims=[];budget=24000;ids=set()
        for r in expanded:
            key=(r['source_id'],r['version'])
            if key not in latest or r['chunk_id'] in ids:continue
            ids.add(r['chunk_id']);text=r['text'][:min(6000,budget)]
            if not text:break
            budget-=len(text);cid='E'+str(len(evidence)+1);m=latest[key]
            ev={'citation_id':cid,'evidence_id':r['chunk_id'],'source_id':key[0],'source_version':key[1],
                'source_url':r['url']+('#'+r['block_id'] if r['block_id'] else ''),'title':r['title'],
                'heading_path':r['heading_path'],'text':text,'governance':m,'knowledge_kind':m.get('knowledge_kind','original')}
            evidence.append(ev)
            # Literal excerpts remain distinct from generated test assertions.
            lines=[l.strip() for l in text.splitlines() if l.strip() and not l.startswith('#')]
            claims.append({'text':'\n'.join(lines[:8])[:1500],'citation_ids':[cid],'mode':'source_excerpt'})
        families={}
        chosen={family(r,current[(r['source_id'],r['version'])]) for r in selected}
        for r in rows:
            key=(r['source_id'],r['version'])
            if key not in latest:continue
            f=family(r,latest[key])
            if f in chosen:families.setdefault(f,set()).add(key)
        conflicts=[{'family':f,'reason':'multiple_unresolved_sources','source_versions':[{'source_id':s,'version':v} for s,v in sorted(values)],
                    'grouping':'path_inferred_not_business_confirmed' if f.startswith('inferred_path:') else 'admin_registered'} for f,values in families.items() if len(values)>1]
        if any(e['governance'].get('state','unknown')=='unknown' for e in evidence):warnings.append('business_validity_unconfirmed')
        if desired=='feature' and not any('02-feature-list' in e['heading_path'].lower() for e in evidence):warnings.append('feature_list_not_retrieved')
        if conflicts:warnings.append('unresolved_version_conflict')
    return response(evidence,claims,warnings,timings,trace,None if evidence else 'no_relevant_evidence',start,conflicts)

def response(evidence,claims,warnings,timings,trace,abstain,start,conflicts=None):
    timings['total']=round((time.monotonic()-start)*1000,3)
    return {'ok':True,'answer_mode':'source_excerpt','answer_status':'abstained' if abstain else 'evidence_found',
      'claims':claims,'evidence':evidence,'warnings':warnings,'conflicts':conflicts or [],'abstain_reason':abstain,
      'coverage':'partial_unverified','retrieval_trace_id':trace,'stage_timings_ms':timings,
      'image_evidence':[]}
