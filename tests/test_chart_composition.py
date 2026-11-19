"""Same-scale source preservation, boundaries and actual consumer integration."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw, ImageChops, ImageStat
import pymupdf

from studio_chart_composition import validate_column_crop, render_column_crop, COLUMN_CROP_SCHEMA


class ChartCompositionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.path=self.root/'chart.png'
        picture=Image.new('RGB',(1000,600),'white');draw=ImageDraw.Draw(picture)
        # Position-dependent pixels expose shifts, stretching and reordered rows.
        for x in range(1000):draw.line((x,0,x,599),fill=(x%256,(x//4)%256,(x//8)%256))
        draw.line((0,220,999,220),fill=(255,0,0),width=3)
        picture.save(self.path)
        self.spec={'y':[.1,.9],'columns':[[.03,.19],[.4,.7],[.86,.97]]}

    def test_preserves_exact_source_pixels_row_alignment_and_visible_joins(self):
        original=self.path.read_bytes();given=copy.deepcopy(self.spec)
        output,info=render_column_crop(self.path,self.spec)
        with Image.open(self.path) as source:
            for record in info['strips']:
                expected=source.crop(record['source_bounds'])
                actual=output.crop(record['output_bounds'])
                self.assertEqual(actual.size,expected.size)
                self.assertEqual(actual.tobytes(),expected.tobytes())
        for left,right in zip(info['strips'],info['strips'][1:]):
            self.assertEqual(right['output_bounds'][0]-left['output_bounds'][2],info['gap_pixels'])
            self.assertEqual(output.getpixel((left['output_bounds'][2],10)),(8,14,21))
        self.assertEqual(info['scale'],1);self.assertEqual(self.spec,given)
        self.assertEqual(self.path.read_bytes(),original)
        self.assertLess(output.width,1000);self.assertEqual(output.height,480)

    def test_strict_ranges_order_counts_and_no_mutation(self):
        from jsonschema import Draft202012Validator
        self.assertEqual(list(Draft202012Validator(COLUMN_CROP_SCHEMA).iter_errors(self.spec)),[])
        self.assertIsNone(validate_column_crop(None))
        self.assertEqual(validate_column_crop({'y':[0,.05],'columns':[[0,.01],[.01,.02]]})['y'],[0,.05])
        invalid=[{},self.spec|{'extra':True},self.spec|{'y':[0,.049]},self.spec|{'y':[0,float('nan')]},
                 self.spec|{'columns':[[0,.5],[.4,.7]]},self.spec|{'columns':[[.5,.8],[0,.2]]},
                 self.spec|{'columns':[[0,.009],[.5,.6]]},self.spec|{'columns':[[0,.4]]},
                 self.spec|{'columns':[[0,True],[.5,.6]]}]
        for spec in invalid:
            with self.subTest(spec=spec),self.assertRaises(ValueError):validate_column_crop(spec)

    def test_pdf_same_global_raster_grid_and_bad_page_rejected(self):
        path=self.root/'chart.pdf'
        with pymupdf.open() as document:
            page=document.new_page(width=500,height=300)
            page.insert_image(page.rect,filename=str(self.path));document.save(path)
        output,info=render_column_crop(path,self.spec,page=1,width=960,height=540)
        with pymupdf.open(path) as document:
            pix=document[0].get_pixmap(matrix=pymupdf.Matrix(info['scale'],info['scale']),alpha=False)
            original=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
        for record in info['strips']:
            difference=ImageChops.difference(output.crop(record['output_bounds']),original.crop(record['raster_bounds']))
            # MuPDF's clipped embedded-image interpolation can differ from a
            # full-page render by 1–2 channel levels at a few edge pixels.
            self.assertTrue(all(high<=2 for _,high in difference.getextrema()))
            self.assertTrue(all(mean<.01 for mean in ImageStat.Stat(difference).mean))
        self.assertEqual(info['source_page'],1)
        for page in [0,2,True]:
            with self.subTest(page=page),self.assertRaises(ValueError):render_column_crop(path,self.spec,page=page)

    def test_review_receives_original_and_same_exportable_composition(self):
        from studio_review_assets import prepare_review_assets
        (self.root/'analysis').mkdir()
        source={'id':'chart','type':'image','file':str(self.path)}
        shot={'kind':'image','source_id':'chart','bbox':[],'focus':[],'column_crop':self.spec}
        plan={'blocks':[{'id':'b1','shots':[shot]}]}
        images,scope=prepare_review_assets(self.root,plan,[source],[],request={'format':'portrait','media_fit':'cover'})
        self.assertEqual(len(scope['originals']),1);self.assertEqual(len(scope['crops']),1)
        expected,metadata=render_column_crop(self.path,self.spec,width=1080,height=1920)
        with Image.open(scope['crops'][0]['path']) as crop:self.assertEqual(crop.tobytes(),expected.tobytes())
        self.assertEqual(scope['crops'][0]['actual_bounds'],metadata)
        self.assertEqual(scope['compositions'][0]['fit'],'contain')
        self.assertTrue(scope['compositions'][0]['protect_content'])
        self.assertIn('column_crop',images[1][0])
        # Neither the original nor composition may be silently omitted.
        with self.assertRaises(ValueError):prepare_review_assets(self.root,plan,[source],[],max_images=1)
        shot['focus']=[.1,.1,.2,.2]
        with self.assertRaises(ValueError):prepare_review_assets(self.root,plan,[source],[])

    def test_preview_endpoint_matches_renderer_and_rejects_unregistered_paths(self):
        import threading
        import shutil
        from urllib.request import urlopen
        from urllib.parse import urlencode
        from urllib.error import HTTPError
        from unittest.mock import patch
        import paper_video as pv
        import studio_server as server
        job=self.root/'jobs'/'sample';(job/'analysis').mkdir(parents=True)
        shutil.copyfile(self.path,job/'chart.png')
        sources=[{'id':'chart','type':'image','file':str(job/'chart.png')},
                 {'id':'outside','type':'image','file':str(self.path)}]
        pv.save_json(job/'analysis/sources.json',sources)
        pv.save_json(job/'request.json',{'format':'portrait'})
        with patch.object(server,'ROOT',self.root):
            http=server.StudioHTTPServer(('127.0.0.1',0),server.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            try:
                base=f'http://127.0.0.1:{http.server_port}'
                query={'job':'sample','id':'chart','column_crop':json.dumps(self.spec)}
                with urlopen(base+'/api/column-preview?'+urlencode(query)) as response:data=json.load(response)
                expected,info=render_column_crop(job/'chart.png',self.spec,width=1080,height=1920)
                from io import BytesIO
                with urlopen(base+data['url']) as response:
                    with Image.open(BytesIO(response.read())) as picture:self.assertEqual(picture.tobytes(),expected.tobytes())
                self.assertEqual(data['provenance'],info)
                for changes in [{'id':'outside'},{'id':'missing'},{'path':str(self.path)},
                                {'column_crop':json.dumps(self.spec|{'columns':[[0,.7],[.6,.9]]})}]:
                    with self.subTest(changes=changes),self.assertRaises(HTTPError) as error:
                        urlopen(base+'/api/column-preview?'+urlencode(query|changes))
                    self.assertEqual(error.exception.code,400)
            finally:http.shutdown();thread.join();http.server_close()

    def test_engine_export_review_and_preview_use_identical_column_pixels(self):
        import paper_video as pv
        import studio_engine as engine
        from studio_review_assets import prepare_review_assets
        for folder in ('assets','analysis'):(self.root/folder).mkdir()
        source={'id':'chart','type':'image','file':str(self.path),'name':'Comparison'}
        shot={'kind':'image','source_id':'chart','in':0,'duration':2,'bbox':[],'focus':[],
              'column_crop':self.spec,'label':'Selected comparison','card_text':'','cue':'','keep_full':False,'diagram':None}
        plan={'title':'Comparison','summary':'A comparison','source_issues':[],'unused_sources':[],
              'blocks':[{'id':'b1','chapter':'Results','narration':'Compare these conditions.','evidence':[],'shots':[shot]}]}
        request={'format':'portrait','layout_style':'immersive','media_fit':'cover'}
        engine.validate(plan,[source])
        project=engine.export(self.root,plan,[source],request)
        exported=project['blocks'][0]['shots'][0]
        _,scope=prepare_review_assets(self.root,plan,[source],[],request=request)
        with Image.open(exported['file']) as actual,Image.open(scope['crops'][0]['path']) as reviewed:
            self.assertEqual(actual.tobytes(),reviewed.tobytes());self.assertEqual(actual.size,reviewed.size)
        self.assertTrue(exported['protect_content']);self.assertNotIn('focus',exported)
        self.assertEqual(exported['column_composition'],scope['crops'][0]['actual_bounds'])
        self.assertEqual(pv.load_json(self.root/'evidence.json')['crops'][0]['kind'],'column_crop')
        self.assertEqual(project['width'],1080);self.assertEqual(project['height'],1920)


if __name__=='__main__':unittest.main()
