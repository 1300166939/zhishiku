"""Bounded Feishu XML parser. Links and comments remain untrusted source data."""
import re
import xml.etree.ElementTree as ET

LAYOUT_CONTAINERS = frozenset({'grid', 'column'})


def parse_document(document):
    xml = document['content']
    if len(xml.encode()) > 8_000_000 or re.search(r'<!DOCTYPE|<!ENTITY', xml, re.I):
        raise ValueError('Unsafe or oversized XML')
    root = ET.fromstring('<root>'+xml+'</root>')
    chunks, media, unsupported, headings = [], [], [], []
    references=[dict(node.attrib) for node in root.iter('cite')]
    inline = {'b','i','u','s','span','a','cite','code','br','mention','equation'}

    def text(node):
        if node.tag == 'cite':
            return node.get('title') or node.get('user-name') or ''.join(node.itertext()).strip()
        return ''.join(node.itertext()).strip()

    def walk(node, inherited=''):
        tag, block = node.tag, node.get('id',inherited)
        if tag == 'cite':
            value=text(node)
            if value:chunks.append({'text':value,'block_id':block,'heading_path':' / '.join(headings),'kind':'reference'})
            return
        if tag in {'img','image','whiteboard','source','file'}:
            media.append({'kind':tag,'block_id':block,'token':node.get('token') or node.get('src'),
                          'mime':None,'coverage':'pending_download'})
            return
        if tag in {'sheet','bitable','iframe','video'}:
            unsupported.append({'kind':tag,'block_id':block})
            return
        if tag == 'table':
            for row in node.iter('tr'):
                value=' | '.join(text(cell) for cell in row if cell.tag in {'td','th'})
                if value:
                    chunks.append({'text':value,'block_id':block,'heading_path':' / '.join(headings), 'kind':'table_row'})
            for descendant in node.iter():
                if descendant.tag in {'img','image','whiteboard','sheet','bitable','source','file','video'}:
                    walk(descendant, block)
            return
        if re.fullmatch('h[1-9]',tag):
            level=int(tag[1]); headings[:] = headings[:level-1]+[text(node)]
        if tag in {'title','p','li','pre','code','checkbox'} or re.fullmatch('h[1-9]',tag):
            # Lists may contain nested blocks. Avoid duplicating their contents.
            value=(node.text or '')+''.join(text(c)+(c.tail or '') for c in node if c.tag in inline)
            value=value.strip()
            if tag=='checkbox' and value:value=('已完成：' if node.get('done')=='true' else '待办：')+value
            for start in range(0,len(value),2000):
                chunks.append({'text':value[start:start+2000],'block_id':block,
                               'heading_path':' / '.join(headings),'kind':tag})
            for child in node:
                if child.tag not in inline:
                    walk(child,block)
            return
        # Layout changes presentation, not the meaning of its child blocks.
        # Walk every child so IDs, images and unsupported resources remain visible.
        if tag not in {'root','ul','ol','quote','blockquote','callout','hr','divider'} | LAYOUT_CONTAINERS:
            unsupported.append({'kind':tag,'block_id':block})
        for child in node:
            walk(child,block)

    walk(root)
    return {'raw_document':document,'chunks':chunks,'media':media,'unsupported':unsupported,'references':references,
            'coverage':{'text':'xml_paragraphs_lists_tables','linked_documents':'reference_only',
                        'comments':'retained_in_raw_not_indexed','ocr':'not_implemented'}}
