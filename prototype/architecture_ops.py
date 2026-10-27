"""Local admin lifecycle operations; isolated restore, manifest checks, dry-run GC."""
import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path

def backup_registry(kb,destination):
    """Requires quiescent writers; vector index is rebuildable, not silently backed up."""
    out=Path(destination);out.mkdir(parents=True,exist_ok=False,mode=0o700)
    with kb.db() as c:
        target=sqlite3.connect(out/'registry.sqlite3')
        try:c.backup(target)
        finally:target.close()
    shutil.copytree(kb.objects.root,out/'objects')
    files={str(f.relative_to(out)):hashlib.sha256(f.read_bytes()).hexdigest() for f in out.rglob('*') if f.is_file()}
    manifest={'created_at':time.time(),'files':files,'vector_restore':'requires_rebuild','writer_condition':'quiescent_required'}
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2));return manifest

def restore_registry(source,destination):
    source=Path(source).resolve();m=json.loads((source/'manifest.json').read_text())
    for name,digest in m['files'].items():
        f=(source/name).resolve()
        if not f.is_relative_to(source) or (source/name).is_symlink() or hashlib.sha256(f.read_bytes()).hexdigest()!=digest:
            raise ValueError('Invalid backup manifest')
    out=Path(destination)
    if out.exists():raise ValueError('Restore destination must not exist')
    shutil.copytree(source,out)
    with sqlite3.connect(out/'registry.sqlite3') as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or c.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('Restore integrity failed')
        from storage import FileObjects
        objects=FileObjects(out/'objects')
        for table in ['versions','assets','recognition_cache','app_vector_cache','api_vector_cache']:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name=?",(table,)).fetchone():
                sql='SELECT DISTINCT object_ref FROM '+table+(" WHERE status<>'revoked'" if table=='versions' else '')
                for (ref,) in c.execute(sql):
                    if ref and (objects.root/ref[:2]/ref).exists():objects.get(ref)
                    elif ref:raise ValueError('Missing restored object')
    return {'registry':'PASS','files':'PASS','vectors':'NOT_RESTORED','destination':str(out)}

def rebuild_vectors(kb,principal):
    """Manual, bounded, resumable via existing stable IDs; no deletion/publication change."""
    allowed={(r['source_id'],r['version']) for r in kb.authorized(principal)};count=0
    with kb.db() as c:
        for source,version in allowed:
            cursor=c.execute('SELECT chunk_id,text FROM chunks WHERE source_id=? AND version=?',(source,version))
            for row in cursor:
                if (source,version) not in {(r['source_id'],r['version']) for r in kb.authorized(principal)}:
                    raise ValueError('Publication changed during rebuild')
                kb.qdrant.upsert(row[0],kb.embedding.embed_text(row[1]).vector,source,version)
                if not kb.qdrant.exists(row[0]):raise ValueError('Vector rebuild reconciliation failed')
                count+=1
    return {'text_vectors':count,'mode':'idempotent_upsert','images':'NOT_REBUILT'}

def gc_plan(kb):
    # All historical references retained. Planning only; no implicit data deletion.
    refs=set()
    with kb.db() as c:
        tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            if not table.replace('_','').isalnum():continue
            columns=[r[1] for r in c.execute('PRAGMA table_info('+table+')')]
            for column in columns:
                if column.endswith('_ref'):
                    refs.update(r[0] for r in c.execute('SELECT '+column+' FROM '+table) if isinstance(r[0],str))
    files=[f.name for f in kb.objects.root.glob('*/*') if f.is_file() and len(f.name)==64]
    return {'mode':'dry_run','unreferenced_objects':[f for f in files if f not in refs],'historical_references_preserved':True}

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('action',choices=['index','backup','restore','rebuild','gc-plan','govern']);p.add_argument('--library',default='common',choices=['common','app','test_api']);p.add_argument('--path');p.add_argument('--destination');a=p.parse_args()
    if a.action=='restore':r=restore_registry(a.path,a.destination)
    else:
        from gateway import knowledge,settings
        cfg=settings();kb=knowledge(cfg,a.library)
        if a.action=='index':
            from evidence_qa import build_index
            r=build_index(kb)
        elif a.action=='backup':r=backup_registry(kb,a.path)
        elif a.action=='rebuild':r=rebuild_vectors(kb,cfg['KB_COLLECTOR_PRINCIPAL'])
        elif a.action=='govern':
            from governance import register
            x=json.loads(Path(a.path).read_text());r=register(kb,**x)
        else:r=gc_plan(kb)
    print(json.dumps(r,ensure_ascii=False))
