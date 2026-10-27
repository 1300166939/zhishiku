import unittest
from ark_vision import ArkVision, VisionError, validate_recognition


class VisionTests(unittest.TestCase):
    def test_boundaries(self):
        with self.assertRaises(VisionError):ArkVision('key','model','http://elsewhere.test')
        v=ArkVision('key','model')
        with self.assertRaises(VisionError):v.recognize(b'not-an-image','image/png')
        with self.assertRaises(VisionError):validate_recognition({'summary':'missing fields'})
        data={'image_type':'chart','summary':'summary','visible_text':['visible'],
              'tables':[],'observed_status':[],'unreadable_regions':[]}
        self.assertEqual(validate_recognition(data),data)
        data['tables']=[{'title':'t','columns':['c'],'rows':[['cell']]}]
        validate_recognition(data)
        data['tables'][0]['rows']=[[123]]
        with self.assertRaises(VisionError):validate_recognition(data)


if __name__=='__main__':unittest.main()
