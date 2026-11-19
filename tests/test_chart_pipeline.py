"""The plan, exported visual and evidence use the same chart composition."""
import copy
from pathlib import Path
import tempfile
import unittest
from PIL import Image,ImageDraw,ImageChops

import paper_video as pv
import studio_engine as engine
from studio_chart_composition import render_column_crop
from tests.test_plan_repair import plan


class ChartPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.job=Path(temporary.name)
        for name in ('assets','analysis','output'):(self.job/name).mkdir()
        self.image=self.job/'assets/source.png'
        image=Image.new('RGB',(1000,400),'white');draw=ImageDraw.Draw(image)
        for x,color in ((20,'red'),(450,'blue'),(850,'green')):
            draw.rectangle((x,35,x+80,365),fill=color)
        image.save(self.image)
        self.source={'id':'chart','type':'image','file':str(self.image),'name':'chart','text':''}
        self.value=plan();block=self.value['blocks'][0];block['evidence']=[]
        block['shots'][0].update(kind='image',source_id='chart',card_text='',bbox=[],focus=[],
            column_crop={'y':[.05,.95],'columns':[[.01,.13],[.44,.56],[.84,.96]]})

    def test_export_pixels_and_recorded_regions_match_composer_with_safe_contain(self):
        engine.validate(self.value,[self.source])
        config=engine.export(self.job,self.value,[self.source],{'format':'portrait','media_fit':'cover','layout_style':'immersive'})
        shot=config['blocks'][0]['shots'][0]
        expected,metadata=render_column_crop(self.image,self.value['blocks'][0]['shots'][0]['column_crop'],width=1080,height=1920)
        with Image.open(shot['file']) as actual:
            self.assertEqual(actual.size,expected.size)
            self.assertIsNone(ImageChops.difference(actual.convert('RGB'),expected).getbbox())
        self.assertTrue(shot['protect_content']);self.assertNotIn('focus',shot)
        self.assertEqual(shot['column_composition'],metadata)
        evidence=pv.load_json(self.job/'evidence.json')['crops'][0]
        self.assertEqual(evidence['source_id'],'chart');self.assertEqual(evidence['strips'],metadata['strips'])
        self.assertEqual((config['width'],config['height']),(1080,1920))

    def test_legacy_validation_does_not_add_new_field_or_change_plan_hash(self):
        value=plan();before=copy.deepcopy(value)
        from tests.test_plan_repair import SOURCES
        engine.validate(value,SOURCES)
        self.assertEqual(value,before)
        self.assertNotIn('column_crop',value['blocks'][0]['shots'][0])

    def test_composition_rejects_other_kinds_and_conflicting_boxes_before_export(self):
        for field,value in (('kind','card'),('bbox',[0,0,1,1]),('focus',[.1,.1,.5,.5])):
            with self.subTest(field=field):
                bad=copy.deepcopy(self.value);bad['blocks'][0]['shots'][0][field]=value
                with self.assertRaisesRegex(ValueError,'同尺度图表拼接'):
                    engine.validate(bad,[self.source])


if __name__=='__main__':unittest.main()
