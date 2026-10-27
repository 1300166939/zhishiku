"""Bounded operational JSON logs; never log request bodies or credentials."""
import json
import logging
import re
import time
import traceback
from pathlib import Path

LOG=logging.getLogger('ai_kb')
if not LOG.handlers:
    handler=logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(message)s'))
    LOG.addHandler(handler)
LOG.setLevel(logging.INFO)
SAFE_ERRORS={'Unsupported report URL','Resource size limit','Package capacity limit','Duplicate package members',
    'Invalid package entry','Package metadata limit','Links-only package must contain HTML and URL manifest only',
    'Legacy image import disabled','Report size limit','Report capacity limit','Invalid screenshot URL',
    'Incomplete screenshot links','Unsupported report HTML structure','Missing report run metadata',
    'Invalid report run identity','Incomplete report run structure','Unrecognized empty screenshot slot',
    'Unexpected screenshot origin','Duplicate report run identity','Merged report capacity limit',
    'HTML mismatch','Incomplete report','Missing text vector','Lexical index coverage incomplete',
    'Qdrant current point query failed','import_queue_full'}

def rss_bytes():
    try:
        return int(next(l.split()[1] for l in Path('/proc/self/status').read_text().splitlines() if l.startswith('VmRSS:')))*1024
    except (OSError,StopIteration):return None

def event(name,**fields):
    LOG.info(json.dumps({'event':name,'time':time.time(),'rss_bytes':rss_bytes(),**fields},ensure_ascii=False,default=str))

def failure(exc,stage,**fields):
    # File/function/line stack excludes source lines, locals and arbitrary exception strings.
    stack=[{'file':Path(f.filename).name,'line':f.lineno,'function':f.name}
           for f in traceback.extract_tb(exc.__traceback__)[-12:]]
    message=str(exc)
    # Only program-authored validation strings and sanitized adapter errors are exposed.
    if message not in SAFE_ERRORS:
        message='Details retained in exception type and stack frames'
    result={'error':type(exc).__name__,'error_code':type(exc).__name__,'stage':stage,'message':message}
    event('operation_failed',**fields,**result,stack=stack)
    return result
