"""Discover configured libraries; actual read/write authority remains in Policy."""
import json
from pathlib import Path
from authorization import DOMAINS, Forbidden, ID

DEFAULT_PATH='/var/lib/ai-knowledge-base/access/library-routes.json'
def load_routes(path=DEFAULT_PATH):
    raw=Path(path).read_bytes()
    if len(raw)>1_000_000:raise ValueError('Route catalog capacity')
    data=json.loads(raw);seen=set();targets=set()
    if not isinstance(data,dict) or data.get('version')!=1 or not isinstance(data.get('libraries'),list):raise ValueError('Invalid route catalog')
    for row in data['libraries']:
        if not isinstance(row,dict) or not ID.fullmatch(row.get('library_id','')) or row['library_id'] in seen:raise ValueError('Invalid library id')
        if row.get('kb_domain') not in DOMAINS or type(row.get('enabled')) is not bool:raise ValueError('Invalid route')
        for key in ['name','owner']:
            if not isinstance(row.get(key),str) or not row[key] or len(row[key])>256:raise ValueError('Invalid description')
        project=row.get('project');target=(row['kb_domain'],project)
        if project is not None and (not isinstance(project,str) or not project or len(project)>256 or project=='*'):raise ValueError('Invalid project')
        if row['kb_domain']=='common' and project is not None:raise ValueError('Invalid common project')
        for key in ['read_credential_id','write_credential_id']:
            if row.get(key) is not None and not ID.fullmatch(row[key]):raise ValueError('Invalid credential reference')
        if type(row.get('rerank_enabled',False)) is not bool:raise ValueError('Invalid rerank flag')
        if 'legacy_projects' in row and (not isinstance(row['legacy_projects'],list) or any(x is not None and not isinstance(x,str) for x in row['legacy_projects'])):raise ValueError('Invalid legacy projects')
        if target in targets:raise ValueError('Duplicate route')
        targets.add(target);seen.add(row['library_id'])
    return data

def visible(policy,cid,catalog):
    result={'ok':True,'catalog_version':catalog['version'],'mine':[],'shared':[],'public':[]}
    for row in catalog['libraries']:
        if not row['enabled']:continue
        actions=[];domain=row['kb_domain'];project=row.get('project')
        for action in ['read','create','update','import']:
            try:
                allowed,_=policy.scope(cid,domain,action)
                if allowed is None or (project is not None and project in allowed):actions.append(action)
            except Forbidden:pass
        if 'read' not in actions:continue
        filters={'kb_domain':domain}
        if project is not None:filters['project']=project
        item={k:row[k] for k in ['library_id','name','owner','kb_domain']}
        item.update(project=project,filters=filters,actions=actions,query_path='/search',rerank_enabled=row.get('rerank_enabled',False))
        key='public' if domain=='common' else ('mine' if any(a!='read' for a in actions) else 'shared')
        result[key].append(item)
    result['private_writable']=result['mine']
    result['private_readonly']=result['shared']
    result['compatibility_aliases']={'mine':'private_writable','shared':'private_readonly'}
    return result

def rerank_allowed(catalog,domain,project):
    exact=[r for r in catalog['libraries'] if r['kb_domain']==domain and r.get('project')==project]
    if exact:return bool(exact[0]['enabled'] and exact[0].get('rerank_enabled',False))
    legacy=next((r for r in catalog['libraries'] if r['kb_domain']==domain and r.get('project') is None),None)
    return bool(legacy and legacy['enabled'] and legacy.get('rerank_enabled',False) and project in legacy.get('legacy_projects',[]))

def credential_rerank_allowed(policy,cid,catalog,domain=None):
    # The compatible direct API has no domain field. An enabled granted library
    # provides this capability; a new project-only credential cannot inherit it.
    domains=[domain] if domain else sorted(DOMAINS)
    for selected in domains:
        try:projects,_=policy.scope(cid,selected)
        except Forbidden:continue
        if projects is None:
            if rerank_allowed(catalog,selected,None):return True
        elif projects and all(rerank_allowed(catalog,selected,p) for p in projects):return True
    return False
