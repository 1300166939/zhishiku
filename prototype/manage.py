"""Trusted local administrative CLI; never exposed through Gateway HTTP."""
import argparse
import json
from pathlib import Path
from gateway import knowledge, settings

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['ingest','publish','unpublish','revoke','query','recognize'])
    parser.add_argument('value')
    parser.add_argument('--library',choices=['common','app'],default='common')
    args=parser.parse_args();cfg=settings();kb=knowledge(cfg,args.library)
    try:
        if args.action=='ingest':
            result=kb.ingest(json.loads(Path(args.value).read_text()))
        elif args.action=='publish':
            result=kb.publish(json.loads(Path(args.value).read_text()))
        elif args.action=='unpublish':
            result=kb.unpublish(args.value)
        elif args.action=='revoke':
            result=kb.revoke(args.value)
        elif args.action=='recognize':
            from derivatives import attach
            result=attach(kb,json.loads(Path(args.value).read_text()))
        else:
            result=kb.query(args.value,cfg['KB_COLLECTOR_PRINCIPAL'])
        print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'error':type(exc).__name__}));raise SystemExit(1)
