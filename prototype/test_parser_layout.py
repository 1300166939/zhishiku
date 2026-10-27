import unittest
from parser import parse_document


class LayoutTests(unittest.TestCase):
    def test_column_order_text_tables_images_and_references(self):
        raw={'content': '<h1 id="h">Report</h1><grid id="g">'
             '<column id="left"><p id="p">Left <b>text</b></p>'
             '<table id="table"><tr><td>A</td><td>B</td></tr></table>'
             '<img id="image" src="image-token"/></column>'
             '<column id="right"><p id="r">Right</p>'
             '<cite id="ref" type="doc" doc-id="source" title="Reference"/>'
             '<grid id="nested"><column id="nested-column"><p>Nested</p></column></grid>'
             '</column></grid>'}
        d=parse_document(raw)
        self.assertFalse(d['unsupported'])
        self.assertEqual([x['text'] for x in d['chunks']],['Report','Left text','A | B','Right','Reference','Nested'])
        self.assertEqual([x['block_id'] for x in d['chunks']],['h','p','table','r','ref','nested-column'])
        self.assertTrue(all(x['heading_path']=='Report' for x in d['chunks']))
        self.assertEqual(d['media'][0]['token'],'image-token')
        self.assertEqual(d['media'][0]['block_id'],'image')
        self.assertEqual(d['references'][0]['doc-id'],'source')
        self.assertEqual(d['raw_document'],raw)

    def test_layout_does_not_hide_unsupported_resources(self):
        d=parse_document({'content':'<grid><column><task task-id="t"/>'
                         '<synced_reference src-token="s"/><bitable token="b"/>'
                         '<unknown-resource/></column></grid>'})
        self.assertEqual([x['kind'] for x in d['unsupported']],
                         ['task','synced_reference','bitable','unknown-resource'])


if __name__=='__main__':unittest.main()
