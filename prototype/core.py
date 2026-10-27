"""SQLite Registry + outbox + private objects + scoped Qdrant retrieval."""
import hashlib
import contextlib
import base64
import json
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
import tempfile
import http.client
from pathlib import Path

from retrieval import bm25, rrf
from storage import FileObjects
import derivatives

COLLECTION = 'kb_common_text_doubao_251215_d2048_v1'
IMAGE_COLLECTION = 'kb_common_image_doubao_251215_d2048_v1'


class Qdrant:
    def __init__(self, key, base='http://127.0.0.1:6333', library='common', query_limit=30):
        if base != 'http://127.0.0.1:6333':
            raise ValueError('Prototype Qdrant must be loopback')
        if type(query_limit) is not int or not 1<=query_limit<=50:raise ValueError('Invalid query limit')
        self.query_limit=query_limit
        self.key, self.base = key, base
        if library not in {'common','app','test_api'}: raise ValueError('Invalid library')
        self.text_collection = 'kb_'+library+'_text_doubao_251215_d2048_v1'
        self.image_collection = 'kb_'+library+'_image_doubao_251215_d2048_v1'

    def request(self, path, method='GET', body=None):
        req = urllib.request.Request(self.base+path, method=method,
            headers={'api-key': self.key, 'Content-Type': 'application/json'},
            data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)['result']

    def upsert(self, cid, vector, source, version, collection=None):
        self.request('/collections/'+(collection or self.text_collection)+'/points?wait=true', 'PUT', {'points':[
            {'id':cid,'vector':vector,'payload':{'source_id':source,'version':version}}]})

    def upsert_many(self,points):
        self.request('/collections/'+self.text_collection+'/points?wait=true','PUT',{'points':points})

    def exists_many(self,ids):
        result=self.request('/collections/'+self.text_collection+'/points','POST',{'ids':ids,'with_payload':False,'with_vector':False})
        return {str(p['id']) for p in result}

    def exists(self, cid, collection=None):
        return self.request('/collections/'+(collection or self.text_collection)+'/points/'+cid).get('id') == cid

    def set_metadata(self,ids,metadata):
        self.request('/collections/'+self.text_collection+'/points/payload?wait=true','POST',
                     {'points':ids,'payload':metadata})

    def search(self, vector, versions, collection=None):
        # Filter before ANN: only currently authorized published versions.
        if not versions:
            return []
        should = [{'must':[{'key':'source_id','match':{'value':s}},
                           {'key':'version','match':{'value':v}}]} for s,v in versions]
        result = self.request('/collections/'+(collection or self.text_collection)+'/points/query', 'POST',
            {'query':vector,'filter':{'should':should},'limit':self.query_limit,'with_payload':False})
        return [str(x['id']) for x in result['points']]

    def search_ids(self,vector,ids):
        """Filter ANN by exact current authorized point IDs; stream bounded request bodies."""
        best={};iterator=iter(ids)
        while True:
            with tempfile.TemporaryFile() as body:
                body.write(json.dumps({'query':vector}).encode()[:-1]+b',"filter":{"must":[{"has_id":[')
                count=0
                for value in iterator:
                    cid=value[0] if not isinstance(value,str) else value
                    if count:body.write(b',')
                    body.write(json.dumps(cid).encode());count+=1
                    if count>=400000:break
                if not count:break
                body.write(b']}]},"limit":'+str(self.query_limit).encode()+b',"with_payload":false}')
                size=body.tell();body.seek(0)
                conn=http.client.HTTPConnection('127.0.0.1',6333,timeout=40)
                try:
                    conn.putrequest('POST','/collections/'+self.text_collection+'/points/query')
                    conn.putheader('api-key',self.key);conn.putheader('Content-Type','application/json')
                    conn.putheader('Content-Length',str(size));conn.endheaders()
                    while True:
                        block=body.read(1024*1024)
                        if not block:break
                        conn.send(block)
                    response=conn.getresponse();value=json.loads(response.read())
                    if response.status!=200:raise RuntimeError('Qdrant current point query failed')
                    for hit in value['result']['points']:
                        cid=str(hit['id']);best[cid]=max(best.get(cid,float('-inf')),hit['score'])
                    best=dict(sorted(best.items(),key=lambda x:x[1],reverse=True)[:self.query_limit])
                finally:conn.close()
                if count<400000:break
        return [cid for cid,_ in sorted(best.items(),key=lambda x:x[1],reverse=True)]

    def delete_source(self, source):
        for collection in (self.text_collection,self.image_collection):
            self.request('/collections/'+(collection or self.text_collection)+'/points/delete?wait=true','POST',
                         {'filter':{'must':[{'key':'source_id','match':{'value':source}}]}})

    def delete_ids(self, ids):
        self.request('/collections/'+self.text_collection+'/points/delete?wait=true','POST',{'points':ids})


class Knowledge:
    def __init__(self, root, embedding, qdrant, collector_principal, library="common", reranker=None):
        if library not in {"common","app","test_api"}: raise ValueError("Invalid library")
        self.library = library
        self.reranker = reranker
        self.image_collection = "kb_"+library+"_image_doubao_251215_d2048_v1"
        if not isinstance(collector_principal, str) or not collector_principal.strip():
            raise ValueError('A configured collector principal is required')
        self.collector_principal = collector_principal
        self.root = Path(root)
        self.root.mkdir(exist_ok=True, parents=True, mode=0o750)
        self.db_path = self.root/'registry.sqlite3'
        self.objects = FileObjects(self.root/'objects')
        self.embedding, self.qdrant = embedding, qdrant
        with self.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS sources(source_id TEXT PRIMARY KEY, active_version TEXT,
                tombstone INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS versions(source_id TEXT, version TEXT, title TEXT, url TEXT,
                object_ref TEXT, content_hash TEXT, proof TEXT, status TEXT,
                PRIMARY KEY(source_id,version));
            CREATE TABLE IF NOT EXISTS chunks(chunk_id TEXT PRIMARY KEY, source_id TEXT, version TEXT,
                text TEXT, block_id TEXT, heading_path TEXT);
            CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, source_id TEXT, version TEXT,
                operation TEXT, status TEXT, attempts INTEGER DEFAULT 0, error TEXT);
            CREATE INDEX IF NOT EXISTS chunk_source ON chunks(source_id,version);
            CREATE TABLE IF NOT EXISTS assets(asset_id TEXT PRIMARY KEY, source_id TEXT, version TEXT,
                object_ref TEXT, block_id TEXT, mime TEXT, kind TEXT, coverage TEXT);
            CREATE TABLE IF NOT EXISTS publications(source_id TEXT, version TEXT, content_hash TEXT,
                audience TEXT NOT NULL, approved_by TEXT NOT NULL, approval_ref TEXT NOT NULL,
                published_at REAL NOT NULL, state TEXT NOT NULL, withdrawn_at REAL,
                PRIMARY KEY(source_id,version),
                FOREIGN KEY(source_id,version) REFERENCES versions(source_id,version));
            ''')
            derivatives.initialize(c)

    @contextlib.contextmanager
    def db(self):
        c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        from evidence_qa import tokens
        c.create_function('qa_tokens',1,tokens,deterministic=True)
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA foreign_keys=ON')
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def proof_valid(proof, principal, content_hash, now=None):
        now = time.time() if now is None else now
        if not isinstance(proof, dict) or not principal:
            return False
        return (proof.get('principal') == principal and proof.get('view_allowed') is True
                and (proof.get('copy_allowed') is True or proof.get('export_allowed') is True)
                and proof.get('content_hash') == content_hash
                and isinstance(proof.get('verified_at'), (int,float))
                and isinstance(proof.get('expires_at'), (int,float))
                and 0 <= now-proof['verified_at'] <= 900
                and now < proof['expires_at'] <= proof['verified_at']+900)

    def ingest(self, bundle):
        source, version = bundle['source_id'], str(bundle['version'])
        raw = json.dumps(bundle['document'],ensure_ascii=False,sort_keys=True).encode()
        digest = hashlib.sha256(raw).hexdigest()
        proof = bundle['proof']
        if (not self.proof_valid(proof,self.collector_principal,digest)
                or proof.get('source_id') != source or str(proof.get('version')) != version):
            raise ValueError('Invalid, expired or mismatched source permission proof')
        chunks = bundle['document']['chunks']
        if (not chunks or len(chunks)>1000 or any(not isinstance(x.get('text'), str)
                or not x['text'] or len(x['text']) > 6000 for x in chunks)):
            raise ValueError('No valid chunks')
        if bundle['document'].get('unsupported'):
            raise ValueError('Incomplete document contains unsupported embedded content')
        media=bundle['document'].get('media',[])
        if len(media)>30 or any(not x.get('data_base64') for x in media):
            raise ValueError('Missing or excessive media')
        object_ref = self.objects.put(raw)
        with self.db() as c:
            old = c.execute('SELECT * FROM sources WHERE source_id=?',(source,)).fetchone()
            if old and old['tombstone']:
                raise ValueError('Source revoked; explicit reauthorization required')
            same = c.execute('SELECT * FROM versions WHERE source_id=? AND version=?',(source,version)).fetchone()
            if same and same['status'] in {'indexed','published'}:
                if same['content_hash'] != digest:
                    raise ValueError('Same version has different content')
                c.execute('UPDATE versions SET proof=? WHERE source_id=? AND version=?',
                          (json.dumps(proof),source,version))
                return {'source_id':source,'version':version,'idempotent':True,'chunks':len(chunks)}
            c.execute('INSERT OR IGNORE INTO sources(source_id) VALUES(?)',(source,))
            c.execute('INSERT OR REPLACE INTO versions VALUES(?,?,?,?,?,?,?,?)',
                      (source,version,bundle['title'],bundle['url'],object_ref,digest,json.dumps(proof),'building'))
            c.execute('INSERT INTO jobs(source_id,version,operation,status) VALUES(?,?,?,?)',
                      (source,version,'index','pending'))
            job = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute('DELETE FROM chunks WHERE source_id=? AND version=?',(source,version))
            c.execute('DELETE FROM assets WHERE source_id=? AND version=?',(source,version))
        count=0
        try:
            for i,chunk in enumerate(chunks):
                cid=str(uuid.uuid5(uuid.NAMESPACE_URL,source+'/'+version+'/'+str(i)+'/'+digest))
                vector=self.embedding.embed_text(chunk['text']).vector
                self.qdrant.upsert(cid,vector,source,version)
                if not self.qdrant.exists(cid):
                    raise ValueError('Vector reconciliation failed')
                with self.db() as c:
                    c.execute('INSERT OR REPLACE INTO chunks VALUES(?,?,?,?,?,?)',
                        (cid,source,version,chunk['text'],chunk.get('block_id',''),chunk.get('heading_path','')))
                count+=1
            for i,asset in enumerate(media):
                data=base64.b64decode(asset['data_base64'],validate=True)
                if len(data)>10_000_000 or asset['mime'] not in {'image/png','image/jpeg','image/webp'}:
                    raise ValueError('Unsupported asset')
                aid=str(uuid.uuid5(uuid.NAMESPACE_URL,source+'/'+version+'/asset/'+str(i)+'/'+digest))
                ref=self.objects.put(data)
                vector=self.embedding.embed_image(data,mime=asset['mime']).vector
                self.qdrant.upsert(aid,vector,source,version,self.image_collection)
                if not self.qdrant.exists(aid,self.image_collection):
                    raise ValueError('Image vector reconciliation failed')
                with self.db() as c:
                    c.execute('INSERT OR REPLACE INTO assets VALUES(?,?,?,?,?,?,?,?)',
                        (aid,source,version,ref,asset.get('block_id',''),asset['mime'],asset['kind'],asset['coverage']))
            if not self.proof_valid(proof,self.collector_principal,digest):
                raise ValueError('Permission proof expired during indexing')
            with self.db() as c:
                current=c.execute('SELECT tombstone FROM sources WHERE source_id=?',(source,)).fetchone()
                if current['tombstone']:
                    raise ValueError('Source revoked during indexing')
                c.execute('UPDATE versions SET status=? WHERE source_id=? AND version=?',('indexed',source,version))
                c.execute('UPDATE jobs SET status=?,attempts=attempts+1 WHERE id=?',('done',job))
            return {'source_id':source,'version':version,'chunks':count,'assets':len(media),
                    'idempotent':False,'status':'indexed_awaiting_publication'}
        except Exception as exc:
            with self.db() as c:
                c.execute('UPDATE versions SET status=? WHERE source_id=? AND version=?',('failed',source,version))
                c.execute('UPDATE jobs SET status=?,attempts=attempts+1,error=? WHERE id=?',('failed',type(exc).__name__,job))
            raise RuntimeError('Index failed; new version not published') from None

    def publish(self, request):
        """Trusted local approval; cannot be supplied through query HTTP."""
        source,version=request['source_id'],str(request['version'])
        if (request.get('audience') != self.library or any(not isinstance(request.get(key),str)
                or not request[key].strip() for key in ['approved_by','approval_ref','content_hash'])):
            raise ValueError('Explicit public-library approval required')
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('''SELECT v.*,s.tombstone FROM versions v JOIN sources s USING(source_id)
                WHERE v.source_id=? AND v.version=?''',(source,version)).fetchone()
            if (not row or row['tombstone'] or row['status'] not in {'indexed','published'}
                    or row['content_hash']!=request['content_hash']):
                raise ValueError('Only reconciled, nonrevoked, matching versions can be published')
            snapshot=self.objects.get(row['object_ref'])
            if hashlib.sha256(snapshot).hexdigest()!=row['content_hash']:
                raise ValueError('Snapshot hash mismatch')
            document=json.loads(snapshot)
            counts=c.execute('SELECT count(*) FROM chunks WHERE source_id=? AND version=?',(source,version)).fetchone()[0]
            assets=c.execute('SELECT count(*) FROM assets WHERE source_id=? AND version=?',(source,version)).fetchone()[0]
            if counts!=len(document['chunks']) or assets!=len(document.get('media',[])):
                raise ValueError('Incomplete version reconciliation')
            for chunk in c.execute('SELECT chunk_id FROM chunks WHERE source_id=? AND version=?',(source,version)):
                if not self.qdrant.exists(chunk[0]):
                    raise ValueError('Missing text index')
            if not c.execute('SELECT 1 FROM chunks WHERE source_id=? AND version=?',(source,version)).fetchone():
                raise ValueError('No indexed text')
            for asset in c.execute('SELECT asset_id,object_ref FROM assets WHERE source_id=? AND version=?',(source,version)):
                self.objects.get(asset['object_ref'])
                if not self.qdrant.exists(asset['asset_id'],self.image_collection):
                    raise ValueError('Missing image index')
            # Switch the publication and active version in the same write transaction.
            c.execute('''INSERT INTO publications VALUES(?,?,?,?,?,?,?, ?,NULL)
                ON CONFLICT(source_id,version) DO UPDATE SET content_hash=excluded.content_hash,
                audience=excluded.audience, approved_by=excluded.approved_by,
                approval_ref=excluded.approval_ref,published_at=excluded.published_at,
                state=excluded.state,withdrawn_at=NULL''',
                (source,version,row['content_hash'],self.library,request['approved_by'],request['approval_ref'],time.time(),'published'))
            c.execute('UPDATE sources SET active_version=? WHERE source_id=?',(version,source))
        return {'source_id':source,'version':version,'audience':self.library,'publication':'published'}

    def unpublish(self, source):
        with self.db() as c:
            c.execute("UPDATE publications SET state='withdrawn',withdrawn_at=? WHERE source_id=?",(time.time(),source))
        return {'source_id':source,'publication':'withdrawn','data_preserved':True}

    def authorized(self, principal):
        # Library membership is supplied only by trusted Gateway authentication.
        if principal != self.collector_principal:
            return []
        with self.db() as c:
            rows=c.execute('''SELECT v.* FROM versions v JOIN sources s ON s.source_id=v.source_id
                AND s.active_version=v.version JOIN publications p ON p.source_id=v.source_id
                AND p.version=v.version AND p.content_hash=v.content_hash
                WHERE s.tombstone=0 AND v.status IN ('indexed','published')
                AND p.state='published' AND p.audience=? ''',(self.library,)).fetchall()
        return [dict(row) for row in rows]

    def query(self, query, principal, top_k=5):
        if not isinstance(query,str) or not query.strip() or len(query)>2000:
            raise ValueError('Query must contain 1 to 2000 characters')
        if type(top_k) is not int or not 1 <= top_k <= 8:
            raise ValueError('top_k must be an integer between 1 and 8')
        versions=self.authorized(principal)
        if not versions:
            return {'evidence':[],'image_evidence':[],'abstain_reason':'no_current_authorized_sources','warnings':[]}
        allowed={(r['source_id'],r['version']) for r in versions}
        with self.db() as c:
            rows=[dict(x) for x in c.execute('SELECT * FROM chunks') if (x['source_id'],x['version']) in allowed]
            assets=[dict(x) for x in c.execute('SELECT * FROM assets') if (x['source_id'],x['version']) in allowed]
        rows += [r for r in derivatives.rows(self) if (r['source_id'],r['version']) in allowed]
        channels={'bm25':[cid for cid,score in bm25(query,rows)]}
        channels['exact']=[x['chunk_id'] for x in rows if query.lower() in x['text'].lower()]
        warnings=[]
        try:
            vector=self.embedding.embed_text(query).vector
            channels['dense']=self.qdrant.search(vector,list(allowed))
            channels['image_dense']=self.qdrant.search(vector,list(allowed),self.image_collection)
        except Exception:
            warnings.append('dense_unavailable_using_lexical')
        # Recheck publication after slow model call: withdrawn/revoked versions cannot return.
        current={(r['source_id'],r['version']) for r in self.authorized(principal)}
        ready={r['chunk_id'] for r in derivatives.rows(self)}
        rows=[r for r in rows if 'asset_id' not in r or r['chunk_id'] in ready]
        by_id={x['chunk_id']:x for x in rows if (x['source_id'],x['version']) in current}
        by_asset={x['asset_id']:x for x in assets if (x['source_id'],x['version']) in current}
        metadata={(r['source_id'],r['version']):r for r in versions}
        from rerank import candidate_limit,rank
        retrieval_trace=str(uuid.uuid4())
        if self.reranker and self.reranker.enabled:
            channels={name:[cid for cid in ids if cid in by_id or cid in by_asset] for name,ids in channels.items()}
        candidates=rrf(channels,candidate_limit(self.reranker,top_k))
        candidates,rerank_scores,ranking,rerank_warnings=rank(self.reranker,query,candidates,
            {cid:r['text'] for cid,r in by_id.items()},top_k,retrieval_trace)
        warnings.extend(rerank_warnings)
        evidence=[]
        image_evidence=[]
        for aid in channels.get('image_dense',[])[:2]:
            if aid not in by_asset:continue
            asset=by_asset[aid];meta=metadata[(asset['source_id'],asset['version'])]
            image_evidence.append({'evidence_id':aid,'kind':'image','asset_id':aid,'mime':asset['mime'],
                'coverage':asset['coverage'],'source_id':asset['source_id'],'source_version':asset['version'],
                'title':meta['title'],'source_url':meta['url']+'#'+asset['block_id'],
                'channels':['text_to_image_dense'],'trust_status':'source_image',
                'permission_mode':'public_library_publication'})
        budget=12000
        for cid,score,trace in candidates:
            if cid in by_asset:
                asset=by_asset[cid];meta=metadata[(asset['source_id'],asset['version'])]
                evidence.append({'evidence_id':cid,'kind':'image','asset_id':cid,'mime':asset['mime'],
                    'coverage':asset['coverage'],'source_id':asset['source_id'],'source_version':asset['version'],
                    'title':meta['title'],'source_url':meta['url']+'#'+asset['block_id'],
                    'score':score,'channels':trace,'trust_status':'source_image',
                    'permission_mode':'public_library_publication'})
                continue
            if cid not in by_id:
                continue
            row=by_id[cid];meta=metadata[(row['source_id'],row['version'])]
            if len(row['text'])>budget:
                continue
            budget-=len(row['text'])
            evidence.append({'evidence_id':cid,'kind':'text','text':row['text'],'source_id':row['source_id'],
                'source_version':row['version'],'title':meta['title'],
                'source_url':meta['url']+('#'+row['block_id'] if row['block_id'] else ''),
                'heading_path':row['heading_path'],'score':score,'channels':trace,
                'trust_status':'source_document','permission_mode':'public_library_publication'})
            if cid in rerank_scores:evidence[-1]['rerank_score']=rerank_scores[cid]
            if 'asset_id' in row:
                evidence[-1].update(trust_status='model_derived',asset_id=row['asset_id'],
                    asset_url='/v1/assets/'+row['asset_id'],derivation_kind=row['kind'],
                    image_sha256=row['image_hash'],model=row['model'],prompt_version=row['prompt_version'],
                    recognition_ref=row['recognition_ref'],mime=row['mime'])
        # Provider calls may race publication changes; derived text must remain ready.
        final={(r['source_id'],r['version']) for r in self.authorized(principal)}
        ready={r['chunk_id'] for r in derivatives.rows(self)}
        evidence=[ev for ev in evidence if (ev['source_id'],ev['source_version']) in final and
                  (ev['trust_status']!='model_derived' or ev['evidence_id'] in ready)]
        image_evidence=[ev for ev in image_evidence if (ev['source_id'],ev['source_version']) in final]
        return {'evidence':evidence,'image_evidence':image_evidence,'warnings':warnings,'ranking':ranking,
                'abstain_reason':None if evidence else 'no_authorized_evidence',
                'retrieval_trace_id':retrieval_trace}

    def asset(self, asset_id, principal):
        allowed={(r['source_id'],r['version']) for r in self.authorized(principal)}
        with self.db() as c:
            row=c.execute('SELECT * FROM assets WHERE asset_id=?',(asset_id,)).fetchone()
        if not row or (row['source_id'],row['version']) not in allowed:
            raise PermissionError('No current authorized asset')
        data=self.objects.get(row['object_ref'])
        if (row['source_id'],row['version']) not in {(r['source_id'],r['version']) for r in self.authorized(principal)}:
            raise PermissionError('Asset publication withdrawn')
        return data,row['mime']

    def query_image(self, image, mime, principal, top_k=5):
        if (not image or len(image)>1_000_000 or mime not in {'image/png','image/jpeg','image/webp'}
                or type(top_k) is not int or not 1<=top_k<=8):
            raise ValueError('Invalid image query')
        versions=self.authorized(principal)
        allowed={(r['source_id'],r['version']) for r in versions}
        if not allowed:
            return {'evidence':[],'abstain_reason':'no_current_authorized_sources'}
        vector=self.embedding.embed_image(image,mime=mime).vector
        ids=self.qdrant.search(vector,list(allowed),self.image_collection)
        current={(r['source_id'],r['version']) for r in self.authorized(principal)}
        metadata={(r['source_id'],r['version']):r for r in versions}
        with self.db() as c:
            assets={r['asset_id']:dict(r) for r in c.execute('SELECT * FROM assets')
                    if (r['source_id'],r['version']) in current}
        evidence=[]
        for aid in ids:
            if aid not in assets:continue
            asset=assets[aid];meta=metadata[(asset['source_id'],asset['version'])]
            evidence.append({'evidence_id':aid,'kind':'image','asset_id':aid,'mime':asset['mime'],
                'coverage':asset['coverage'],'source_id':asset['source_id'],'source_version':asset['version'],
                'source_url':meta['url']+'#'+asset['block_id'],'title':meta['title'],'channels':['image_dense'],
                'trust_status':'source_image','permission_mode':'public_library_publication'})
            if len(evidence)==top_k:break
        return {'evidence':evidence,'abstain_reason':None if evidence else 'no_authorized_evidence'}

    def additional_object_reference(self,c,ref):
        return False

    def revoke(self, source):
        with self.db() as c:
            c.execute('UPDATE sources SET tombstone=1 WHERE source_id=?',(source,))
            c.execute("UPDATE publications SET state='withdrawn',withdrawn_at=? WHERE source_id=?",(time.time(),source))
            c.execute('INSERT INTO jobs(source_id,operation,status) VALUES(?,?,?)',(source,'delete','pending'))
            job=c.execute('SELECT last_insert_rowid()').fetchone()[0]
        try:
            self.qdrant.delete_source(source)
            with self.db() as c:
                refs=[r[0] for r in c.execute('SELECT object_ref FROM versions WHERE source_id=?',(source,))]
                refs += [r[0] for r in c.execute('SELECT object_ref FROM assets WHERE source_id=?',(source,))]
                refs += derivatives.cleanup(c,source)
                c.execute('DELETE FROM chunks WHERE source_id=?',(source,))
                c.execute('DELETE FROM assets WHERE source_id=?',(source,))
                c.execute('UPDATE jobs SET status=?,attempts=attempts+1 WHERE id=?',('done',job))
                for ref in refs:
                    shared=c.execute('SELECT 1 FROM versions WHERE object_ref=? AND source_id<>?',(ref,source)).fetchone()
                    shared=shared or c.execute('SELECT 1 FROM assets WHERE object_ref=? AND source_id<>?',(ref,source)).fetchone()
                    shared=shared or c.execute('SELECT 1 FROM recognition_cache WHERE object_ref=?',(ref,)).fetchone()
                    shared=shared or self.additional_object_reference(c,ref)
                    if not shared:
                        self.objects.delete(ref)
                # Retain version metadata and approval audit; objects have been removed.
                c.execute("UPDATE versions SET status='revoked' WHERE source_id=?",(source,))
            return {'tombstone':True,'cleanup':'done'}
        except Exception:
            with self.db() as c:
                c.execute('UPDATE jobs SET status=?,attempts=attempts+1 WHERE id=?',('failed',job))
            return {'tombstone':True,'cleanup':'failed_retry_required'}
