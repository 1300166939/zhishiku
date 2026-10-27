"""Team API contract; domains select existing libraries, without copying knowledge."""
from app_library import validate_filters


class ContractError(ValueError):
    pass


def domains():
    return {'ok': True, 'domains': [
        {'kb_domain': 'common', 'name': '公共知识库', 'search': True,
         'image_search': True, 'report_import': False},
        {'kb_domain': 'test_api', 'name': 'API 专属知识库', 'search': True,
         'image_search': False, 'text_import': True,
         'doc_types': sorted(__import__('api_library').DOC_TYPES)},
        {'kb_domain': 'app', 'name': 'App 专属知识库', 'search': True,
         'image_search': False, 'report_import': True,'storage_mode':'links_only',
         'screenshot_links':True,'map_import':True,
         'doc_types':['app_page_rule_chunk','app_execution_report_chunk']}],
        'embedding_model': 'doubao-embedding-vision-251215', 'dimension': 2048}


def search_request(body, image=False):
    fields = {'image_base64', 'mime'} if image else {'query'}
    if not isinstance(body, dict) or set(body) - (fields | {'top_k', 'filters'}):
        raise ContractError('invalid_request')
    filters = body.get('filters')
    if not isinstance(filters, dict) or 'kb_domain' not in filters:
        raise ContractError('kb_domain_required')
    filters = dict(filters)
    domain = filters.pop('kb_domain')
    if not isinstance(domain, str) or domain not in {'common', 'app','test_api'}:
        raise ContractError('unknown_kb_domain')
    if domain == 'common' and filters:
        raise ContractError('unsupported_filter')
    if domain == 'test_api':
        if image:raise ContractError('api_image_search_unsupported')
        from api_library import validate_filters as api_filters
        try:filters=api_filters(filters)
        except ValueError as exc:raise ContractError('unsupported_filter') from exc
    if domain == 'app':
        # The old API names the environment field env; preserve its exact semantics.
        if 'env' in filters:
            if 'environment' in filters and filters['environment'] != filters['env']:
                raise ContractError('conflicting_environment_filter')
            filters['environment'] = filters.pop('env')
        try:
            validate_filters(filters)
        except ValueError as exc:
            raise ContractError('unsupported_filter') from exc
    top_k = body.get('top_k', 5)
    if type(top_k) is not int or not 1 <= top_k <= (50 if domain=='test_api' else 8):
        raise ContractError('invalid_top_k')
    return domain, filters, top_k


def search_response(result, domain):
    def convert(ev):
        # Keep every evidence field: image/provenance/context must survive adaptation.
        metadata = dict(ev.get('metadata', {}))
        metadata.update(kb_domain=domain)
        for key in ('source_id', 'source_version', 'source_url', 'title', 'trust_status',
                    'asset_id', 'source_locator', 'heading_path'):
            if key in ev and not (domain=='test_api' and key in metadata):
                metadata[key] = ev[key]
        item = {**ev, 'id': ev.get('original_id',ev['evidence_id']) if domain=='test_api' else ev['evidence_id'], 'text': ev.get('text', ''),
                'score': ev.get('score'), 'metadata': metadata}
        if ev.get('asset_id'):
            item.setdefault('asset_url', '/v1/assets/' + ('app/' if domain == 'app' else '') + ev['asset_id'])
        return item
    return {'ok': True, 'kb_domain': domain,
            'results': [convert(ev) for ev in result.get('evidence', [])],
            'image_results': [convert(ev) for ev in result.get('image_evidence', [])],
            'warnings': result.get('warnings', []),
            'abstain_reason': result.get('abstain_reason'),
            'retrieval_trace_id': result.get('retrieval_trace_id'),
            'ranking': result.get('ranking'),
            'score_type': 'image_dense_rank' if 'retrieval_trace_id' not in result else 'rrf'}


def import_response(job):
    return {**job, 'status_url': '/imports/' + job['job_id']}


POST_ALIASES = {
    '/imports/app/packages': '/v1/app/report-packages',
    '/imports/app': '/v1/app/import-jobs',
    '/reports/app': '/v1/app/reports',
}
