"""Plan review sees actual detail-view stages and explicit budget omissions."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from studio_review_assets import prepare_review_assets


class VisualBeatsReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.job=Path(self.temporary.name).resolve()
        (self.job/'analysis').mkdir()
        (self.job/'assets').mkdir()
        self.path=self.job/'assets/figure.png'
        source=Image.new('RGB',(600,300),'red')
        draw=ImageDraw.Draw(source)
        draw.rectangle((200,0,399,299),fill='blue')
        draw.rectangle((400,0,599,299),fill='lime')
        source.save(self.path)
        self.sources=[{'id':'i1','type':'image','file':str(self.path),'name':'Figure'}]
        self.beats=[{'cue':'','bbox':[0,0,1,1],'label':'完整图'},
                    {'cue':'左侧','bbox':[0,0,1/3,1],'label':'左侧区域'},
                    {'cue':'右侧','bbox':[2/3,0,1,1],'label':'右侧区域'}]
        self.shot={'kind':'image','source_id':'i1','bbox':[],'focus':[],
                   'visual_beats':self.beats}
        self.plan={'blocks':[{'id':'b1','narration':'先观察完整图，再看左侧，最后看右侧。',
                             'shots':[self.shot]}]}
        self.request={'format':'custom','width':960,'height':540}

    def prepare(self, **kwargs):
        return prepare_review_assets(self.job,self.plan,self.sources,[],request=self.request,**kwargs)

    def test_three_stages_replace_the_plain_composition_and_show_distinct_detail(self):
        before=copy.deepcopy((self.plan,self.sources))
        with patch('studio_review_assets._composition',side_effect=AssertionError('plain composition is not the stage view')):
            images,coverage=self.prepare()
        self.assertEqual(len(images),4)  # original plus all three stage views
        stages=coverage['compositions']
        self.assertEqual([stage['stage_index'] for stage in stages],[0,1,2])
        self.assertEqual([stage['cue'] for stage in stages],['','左侧','右侧'])
        self.assertEqual(coverage['omitted_compositions'],[])
        colors=[]
        for stage in stages:
            self.assertEqual(stage['kind'],'visual_beat')
            self.assertEqual(stage['shots'],[{'block_id':'b1','shot_index':0}])
            self.assertEqual(stage['canvas'],[960,540])
            self.assertIn('stage index is not time',stage['scope'])
            self.assertNotIn('start',stage)
            self.assertNotIn('seconds',stage)
            self.assertIn('未计时',stage['label'])
            with Image.open(stage['path']) as picture:
                # Centre of the detail panel: full image's blue centre, then
                # genuinely red/green crops. A reused full-image preview fails.
                colors.append(picture.convert('RGB').getpixel((662,270)))
                self.assertEqual(picture.getpixel((90,260)),(255,0,0),
                                 'the source overview must remain visible')
        self.assertEqual(colors,[(0,0,255),(255,0,0),(0,255,0)])
        self.assertEqual((self.plan,self.sources),before)

    def test_all_six_stages_are_attached_when_budget_allows(self):
        self.beats[:]=[{'cue':'' if index==0 else f'对象{index}',
                       'bbox':[0,0,1,1] if index==0 else [0,0,.5,1],
                       'label':f'区域{index}'} for index in range(6)]
        self.plan['blocks'][0]['narration']='观察全图。对象1。对象2。对象3。对象4。对象5。'
        images,coverage=self.prepare(max_images=7)
        self.assertEqual(len(images),7)
        self.assertEqual([item['stage_index'] for item in coverage['compositions']],list(range(6)))
        self.assertEqual(coverage['omitted_compositions'],[])

    def test_small_budget_records_every_omitted_stage_instead_of_claiming_full_coverage(self):
        images,coverage=self.prepare(max_images=2)
        self.assertEqual(len(images),2)
        self.assertEqual([item['stage_index'] for item in coverage['compositions']],[0])
        omitted=coverage['omitted_compositions']
        self.assertEqual([item['stage_index'] for item in omitted],[1,2])
        self.assertEqual([item['cue'] for item in omitted],['左侧','右侧'])
        self.assertTrue(all('not attached or inspected' in item['reason'] for item in omitted))
        saved=json.loads((self.job/'analysis/review_assets.json').read_text(encoding='utf8'))
        self.assertEqual(saved,coverage)

    def test_relative_registered_source_file_is_resolved_for_preview(self):
        self.sources[0]['file']='assets/figure.png'
        _,coverage=self.prepare()
        self.assertEqual(len(coverage['compositions']),3)
        self.assertEqual(self.sources[0]['file'],'assets/figure.png')

    def test_pdf_stages_show_the_actual_detail_and_keep_the_full_page_attachment(self):
        import pymupdf
        pdf=self.job/'assets/figure.pdf'
        with pymupdf.open() as document:
            page=document.new_page(width=600,height=300)
            page.insert_image(page.rect,filename=str(self.path))
            document.save(pdf)
        self.sources[0].update(type='page',file=str(pdf),page=1)
        self.shot.update(kind='page',bbox=[0,0,1,1])
        images,coverage=self.prepare()
        self.assertEqual(len(images),4)
        self.assertEqual(coverage['originals'][0]['page'],1)
        with Image.open(coverage['compositions'][1]['path']) as picture:
            self.assertEqual(picture.getpixel((662,270)),(255,0,0))

    def test_portrait_stage_previews_use_the_selected_canvas(self):
        self.request={'format':'custom','width':540,'height':960}
        _,coverage=self.prepare()
        for item in coverage['compositions']:
            self.assertEqual(item['canvas'],[540,960])
            with Image.open(item['path']) as picture:
                self.assertEqual(picture.size,(540,960))

    def test_different_stage_sequences_for_same_source_are_not_deduplicated(self):
        second=copy.deepcopy(self.shot)
        second['visual_beats'][1]['bbox']=[1/3,0,2/3,1]
        self.plan['blocks'][0]['shots'].append(second)
        _,coverage=self.prepare()
        self.assertEqual(len(coverage['originals']),1)
        self.assertEqual(len(coverage['compositions']),6)
        self.assertEqual([item['shots'][0]['shot_index'] for item in coverage['compositions']],[0,0,0,1,1,1])

    def test_identical_stage_sequences_share_images_with_both_shot_references(self):
        self.plan['blocks'][0]['shots'].append(copy.deepcopy(self.shot))
        _,coverage=self.prepare()
        self.assertEqual(len(coverage['compositions']),3)
        for item in coverage['compositions']:
            self.assertEqual(item['shots'],[{'block_id':'b1','shot_index':0},{'block_id':'b1','shot_index':1}])

    def test_conflicting_highlight_is_not_silently_dropped(self):
        self.shot['focus']=[0,0,.3,1]
        with self.assertRaisesRegex(ValueError,'不能同时'):
            self.prepare()


if __name__=='__main__':
    unittest.main()
