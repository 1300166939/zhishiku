"""Quiescent single-node backup. Does not replace an offsite backup target."""
import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path
from core import COLLECTION, IMAGE_COLLECTION
from gateway import knowledge, settings


def backup(kb, destination):
    out=Path(destination);out.mkdir(mode=0o750,parents=True,exist_ok=False)
    with kb.db() as source:
        target=sqlite3.connect(out/'registry.sqlite3')
        try:source.backup(target)
        finally:target.close()
    shutil.copytree(kb.objects.root,out/'objects')
    snapshots={}
    for collection in (kb.qdrant.text_collection,kb.qdrant.image_collection):
        snapshot=kb.qdrant.request('/collections/'+collection+'/snapshots','POST')['name']
        source=Path('/var/lib/ai-knowledge-base/snapshots')/collection/snapshot
        target=out/snapshot;shutil.copy2(source,target)
        snapshots[collection]=snapshot
    manifest={'created_at':time.time(),'scope':'local_single_node',
              'requires':'pause trusted ingest/revoke while copying','qdrant_snapshots':snapshots,'files':{}}
    for file in out.rglob('*'):
        if file.is_file():manifest['files'][str(file.relative_to(out))]=hashlib.sha256(file.read_bytes()).hexdigest()
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2))
    return manifest


def restore_check(destination, isolated):
    source=Path(destination);manifest=json.loads((source/'manifest.json').read_text())
    for name,digest in manifest['files'].items():
        if hashlib.sha256((source/name).read_bytes()).hexdigest()!=digest:
            raise ValueError('Backup checksum mismatch')
    target=Path(isolated);shutil.copytree(source,target)
    db=sqlite3.connect(target/'registry.sqlite3')
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Registry integrity failed')
        refs=[r[0] for r in db.execute("SELECT object_ref FROM versions WHERE status<>'revoked'")]
        refs += [r[0] for r in db.execute('SELECT object_ref FROM assets')]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='recognition_cache'").fetchone():
            refs += [r[0] for r in db.execute('SELECT object_ref FROM recognition_cache')]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='app_entities'").fetchone():
            refs += [r[0] for r in db.execute('SELECT parent_ref FROM app_entities')]
            refs += [r[0] for r in db.execute('SELECT manifest_ref FROM app_reports')]
            refs += [r[0] for r in db.execute('SELECT html_ref FROM app_reports')]
            refs += [r[0] for r in db.execute("SELECT object_ref FROM app_downloads WHERE status='downloaded'")]
            refs += [r[0] for r in db.execute('SELECT object_ref FROM app_vector_cache')]
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Registry reference integrity failed')
        from storage import FileObjects
        objects=FileObjects(target/'objects')
        for ref in set(refs):objects.get(ref)
        return {'registry_integrity':'PASS','object_checksum':'PASS','references':len(set(refs)),
                'snapshot_checksums':'PASS','qdrant_restore_query':'NOT_RUN','offsite_restore':'NOT_RUN'}
    finally:db.close()


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('destination');parser.add_argument('--restore-check')
    args=parser.parse_args()
    if args.restore_check:result=restore_check(args.destination,args.restore_check)
    else:result={'files':len(backup(knowledge(settings()),args.destination)['files'])}
    print(json.dumps(result))
