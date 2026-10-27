"""Image recognition companions; published source snapshots remain immutable."""
import hashlib
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'integration'))
from ark_vision import validate_recognition


def initialize(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS recognition_cache(
        cache_id TEXT PRIMARY KEY, image_hash TEXT NOT NULL, profile TEXT NOT NULL,
        model TEXT NOT NULL, prompt_version TEXT NOT NULL, object_ref TEXT NOT NULL,
        created_at REAL NOT NULL, UNIQUE(image_hash,profile));
    CREATE TABLE IF NOT EXISTS asset_derivatives(
        derivative_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, source_id TEXT NOT NULL,
        version TEXT NOT NULL, cache_id TEXT NOT NULL, kind TEXT NOT NULL,
        text TEXT NOT NULL, status TEXT NOT NULL,
        FOREIGN KEY(asset_id) REFERENCES assets(asset_id),
        FOREIGN KEY(cache_id) REFERENCES recognition_cache(cache_id));
    CREATE INDEX IF NOT EXISTS derivative_source ON asset_derivatives(source_id,version,status);
    ''')


def fragments(value):
    candidates = []
    paragraph = ''
    for text in value['visible_text']:
        if paragraph and len(paragraph)+len(text)>1800:
            candidates.append(('image_ocr',paragraph));paragraph=''
        paragraph += ('\n' if paragraph else '')+text
    if paragraph:
        candidates.append(('image_ocr',paragraph))
    candidates += [('image_summary',value['summary'])]
    for table in value['tables']:
        header = ' | '.join(table['columns'])
        candidates += [('image_table', table['title']+'\n'+header+'\n'+' | '.join(row))
                       for row in table['rows']]
    candidates += [('image_observed',text) for text in value['observed_status']]
    out = []
    for kind,text in candidates:
        for start in range(0,len(text),2000):
            if text[start:start+2000].strip():
                out.append((kind,text[start:start+2000]))
    if not out or len(out)>300:
        raise ValueError('Recognition exceeds derivative limits')
    return out


def attach(kb, request):
    aid = request['asset_id']
    result = request['result']
    value = validate_recognition(result['recognition'])
    image_hash = request['image_sha256']
    if (result.get('image_sha256') != image_hash or not all(isinstance(result.get(k),str)
            and result[k] for k in ['model','profile','prompt_version','request_id'])
            or len(result['profile'])!=64 or result.get('trust_status')!='model_derived'):
        raise ValueError('Invalid recognition provenance')
    with kb.db() as c:
        asset = c.execute('SELECT * FROM assets WHERE asset_id=?',(aid,)).fetchone()
        if not asset or (asset['source_id'],asset['version']) not in {
                (r['source_id'],r['version']) for r in kb.authorized(kb.collector_principal)}:
            raise PermissionError('Only currently published assets can be enriched')
        asset = dict(asset)
    if asset['object_ref'] != image_hash:
        raise ValueError('Recognition does not match original image')
    kb.objects.get(image_hash)
    cid = hashlib.sha256((image_hash+':'+result['profile']).encode()).hexdigest()
    with kb.db() as c:
        cached = c.execute('SELECT * FROM recognition_cache WHERE cache_id=?',(cid,)).fetchone()
    cache_hit = cached is not None
    if cached:
        canonical = json.loads(kb.objects.get(cached['object_ref']))
        if (canonical['image_sha256']!=image_hash or canonical['profile']!=result['profile']
                or canonical['model']!=result['model']):
            raise ValueError('Cache provenance mismatch')
        value = validate_recognition(canonical['recognition'])
    else:
        ref = kb.objects.put(json.dumps(result,ensure_ascii=False,sort_keys=True).encode())
        with kb.db() as c:
            c.execute('INSERT INTO recognition_cache VALUES(?,?,?,?,?,?,?)',
                (cid,image_hash,result['profile'],result['model'],result['prompt_version'],ref,time.time()))
    parts = fragments(value)
    ids = [str(uuid.uuid5(uuid.NAMESPACE_URL,aid+'/'+cid+'/'+str(i))) for i in range(len(parts))]
    with kb.db() as c:
        existing = c.execute('SELECT derivative_id,status FROM asset_derivatives WHERE asset_id=? AND cache_id=?',
                             (aid,cid)).fetchall()
    if (len(existing)==len(parts) and all(r['status']=='ready' for r in existing)
            and all(kb.qdrant.exists(did) for did in ids)):
        return {'asset_id':aid,'cache_hit':True,'idempotent':True,'derivative_chunks':len(parts)}
    with kb.db() as c:
        for did,(kind,text) in zip(ids,parts):
            c.execute('INSERT OR REPLACE INTO asset_derivatives VALUES(?,?,?,?,?,?,?,?)',
                      (did,aid,asset['source_id'],asset['version'],cid,kind,text,'pending'))
    try:
        for did,(_,text) in zip(ids,parts):
            vector = kb.embedding.embed_text(text).vector
            kb.qdrant.upsert(did,vector,asset['source_id'],asset['version'])
            if not kb.qdrant.exists(did):
                raise ValueError('Derivative vector reconciliation failed')
        with kb.db() as c:
            c.execute('BEGIN IMMEDIATE')
            active = c.execute('''SELECT 1 FROM assets a JOIN sources s ON s.source_id=a.source_id
                JOIN publications p ON p.source_id=s.source_id AND p.version=s.active_version
                JOIN versions v ON v.source_id=p.source_id AND v.version=p.version
                WHERE a.asset_id=? AND a.version=s.active_version AND a.object_ref=?
                AND s.tombstone=0 AND p.state='published' AND p.audience=?
                AND p.content_hash=v.content_hash''',(aid,image_hash,kb.library)).fetchone()
            if not active:
                raise PermissionError('Image withdrawn during enrichment')
            c.execute("UPDATE asset_derivatives SET status='superseded' WHERE asset_id=? AND cache_id<>?",(aid,cid))
            c.execute("UPDATE asset_derivatives SET status='ready' WHERE asset_id=? AND cache_id=?",(aid,cid))
        return {'asset_id':aid,'cache_hit':cache_hit,'idempotent':False,'derivative_chunks':len(parts)}
    except Exception:
        with kb.db() as c:
            c.execute("UPDATE asset_derivatives SET status='failed' WHERE asset_id=? AND cache_id=?",(aid,cid))
        try:
            kb.qdrant.delete_ids(ids)
        except Exception:
            pass  # Failed points remain invisible and are overwritten on stable-ID retry.
        raise RuntimeError('Recognition index failed; published source preserved') from None


def rows(kb):
    with kb.db() as c:
        return [dict(r) for r in c.execute('''SELECT d.derivative_id AS chunk_id,d.source_id,d.version,
            d.text,a.block_id,a.asset_id,a.mime,a.object_ref AS image_hash,
            d.kind,r.model,r.prompt_version,r.profile,r.object_ref AS recognition_ref,
            '图片识别' AS heading_path
            FROM asset_derivatives d JOIN assets a ON d.asset_id=a.asset_id
            AND d.source_id=a.source_id AND d.version=a.version
            JOIN recognition_cache r ON r.cache_id=d.cache_id AND r.image_hash=a.object_ref
            WHERE d.status='ready' ''')]


def cleanup(c,source):
    c.execute('DELETE FROM asset_derivatives WHERE source_id=?',(source,))
    unused = c.execute('''SELECT cache_id,object_ref FROM recognition_cache r WHERE NOT EXISTS
        (SELECT 1 FROM asset_derivatives d WHERE d.cache_id=r.cache_id)''').fetchall()
    for row in unused:
        c.execute('DELETE FROM recognition_cache WHERE cache_id=?',(row['cache_id'],))
    return [r['object_ref'] for r in unused]
