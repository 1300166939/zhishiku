"""Deterministic App report adapter. All hidden panes and raw log records retained."""
import hashlib
import re
from urllib.parse import urljoin, urlparse

PARSER_VERSION = 'app-html-v1'

def parse_report(raw, url):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw, 'html.parser')
    batch = urlparse(url).path.rstrip('/').split('/')[-2]
    report = {'batch_id':batch, 'title':soup.title.get_text(' ',strip=True), 'source_url':url,
              'html_sha256':hashlib.sha256(raw).hexdigest(), 'parser_version':PARSER_VERSION,
              'app_build':'unknown', 'environment':'unknown', 'runs':[], 'coverage_mismatches':[]}
    panes=soup.select('.case-pane[data-case-key]')
    single=not panes
    if single:
        if len(soup.select('.timeline'))!=1 or len(soup.select('.runcontent'))!=1:
            raise ValueError('Unsupported report HTML structure')
        panes=[soup]
    for pane in panes:
        meta = {}
        for chip in pane.select('.hd-meta .chip'):
            label = chip.find('b')
            if label:
                meta[label.get_text(strip=True)] = chip.get_text(' ',strip=True).removeprefix(label.get_text(strip=True)).strip()
        if not all(meta.get(k) for k in ['Run ID','用例 ID','步数']):
            raise ValueError('Missing report run metadata')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',meta['Run ID']):
            raise ValueError('Invalid report run identity')
        heading=pane.find('h1' if single else 'h2')
        if not heading or not pane.select_one('.timeline') or not pane.select_one('.runcontent'):
            raise ValueError('Incomplete report run structure')
        run = {'batch_id':batch,'case_key':urlparse(url).path.split('/')[-1][:-5] if single else pane['data-case-key'], 'run_id':meta['Run ID'],
               'case_id':meta['用例 ID'], 'platform':meta.get('平台','unknown'),
               'device':meta.get('设备','unknown'), 'started_at':meta.get('开始'), 'ended_at':meta.get('结束'),
               'reported_reason':meta.get('状态原因','unknown'), 'review_status':'unreviewed',
               'title':heading.get_text(' ',strip=True), 'declared_steps':int(meta['步数']),
               'definition':pane.select_one('.runcontent').get_text('\n',strip=True),
               'failure_summary':pane.select_one('.failure-summary').get_text('\n',strip=True) if pane.select_one('.failure-summary') else '',
               'steps':[], 'logs':[], 'unassigned_log_indices':[], 'app_build':'unknown','environment':'unknown'}
        run['reported_status'] = 'failed' if run['reported_reason'] in {'assert_failed','executor_error'} else 'success'
        pending=[]; step_num=None; markers=[]
        for row in pane.select_one('.timeline').find_all(recursive=False):
            classes=row.get('class',[])
            if 'tl-log' in classes:
                body=row.select_one('.tl-body').get_text('\n',strip=True)
                log={'time':row.select_one('.tl-time').get_text(strip=True), 'text':body,
                     'level':next((x[4:] for x in classes if x.startswith('lvl-')),'unknown')}
                idx=len(run['logs']);run['logs'].append(log)
                marker=re.search(r'━━\s*第\s*(\d+)\s*步\s*━━',body)
                if marker:
                    if pending and step_num is not None:run['unassigned_log_indices']+=pending;pending=[]
                    step_num=int(marker.group(1));markers.append(step_num)
                pending.append(idx)
            elif 'tl-step' in classes:
                n=int(re.search(r'\d+',row.select_one('.tl-step-idx').get_text()).group())
                shots=[]
                missing=[]
                for shot in row.select('.tl-shot'):
                    role=shot.find('h5').get_text(strip=True)
                    role='before' if role=='操作前' else 'after' if role=='操作后' else role
                    image=shot.find('img')
                    if image is None:
                        if not shot.select_one('.no-img'):raise ValueError('Unrecognized empty screenshot slot')
                        missing.append({'role':role,'status':'missing','url':None});continue
                    src=urljoin(url,image['src']);parsed=urlparse(src)
                    if parsed.scheme!='https' or parsed.hostname!=urlparse(url).hostname or parsed.username or parsed.password:
                        raise ValueError('Unexpected screenshot origin')
                    shots.append({'url':src,'role':role})
                mismatch=step_num is not None and step_num!=n
                run['steps'].append({'step_index':n,'time':row.select_one('.tl-time').get_text(strip=True),
                    'log_indices':list(pending),'screenshots':shots,'marker_step_index':step_num,
                    'log_association':'mismatch' if mismatch else 'marker_and_render_order',
                    'step_id':run['run_id']+':'+str(n)})
                if missing:run['steps'][-1]['missing_screenshot_slots']=missing
                pending=[];step_num=None
        run['unassigned_log_indices']+=pending
        run['rendered_steps']=len(run['steps']);run['action_markers']=markers
        if run['declared_steps']!=run['rendered_steps']:
            report['coverage_mismatches'].append({'case_id':run['case_id'],'run_id':run['run_id'],
                'declared':run['declared_steps'],'rendered':run['rendered_steps']})
        report['runs'].append(run)
    if len({r['run_id'] for r in report['runs']})!=len(report['runs']):
        raise ValueError('Duplicate report run identity')
    # Old reports retain their exact manifest and source/version identity.
    extended=any(s.get('missing_screenshot_slots') for r in report['runs'] for s in r['steps']) or any(
        len({s['step_index'] for s in r['steps']})!=len(r['steps']) for r in report['runs'])
    if extended:
        report['parser_version']='app-html-v2'
        report['step_index_semantics']='chronological_occurrence_within_run'
        for run in report['runs']:
            attempt=1;previous=None
            for ordinal,step in enumerate(run['steps'],1):
                original=step['step_index']
                if previous is not None and original<=previous:attempt+=1
                step.update(step_index=ordinal,source_step_index=original,attempt_index=attempt,
                            step_id=run['run_id']+':'+str(ordinal))
                step['screenshot_slots']=[{**x,'status':'present'} for x in step['screenshots']]+step.pop('missing_screenshot_slots',[])
                step['screenshot_slots'].sort(key=lambda x: {'before':0,'after':1}.get(x['role'],2))
                previous=original
            run['attempt_count']=attempt
    urls=[x['url'] for r in report['runs'] for s in r['steps'] for x in s['screenshots']]
    report['counts']={'runs':len(report['runs']),'steps':sum(r['rendered_steps'] for r in report['runs']),
                      'logs':sum(len(r['logs']) for r in report['runs']),'screenshot_references':len(urls),
                      'screenshot_urls':len(set(urls)), 'declared_steps':sum(r['declared_steps'] for r in report['runs'])}
    if extended:
        report['counts']['missing_screenshot_slots']=sum(x['status']=='missing' for r in report['runs'] for s in r['steps'] for x in s['screenshot_slots'])
        report['counts']['restarted_runs']=sum(r['attempt_count']>1 for r in report['runs'])
    return report
