import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from studio_media_catalog import deduplicate_new_sources, file_digest, coverage_summary
from studio_research import Research


class MediaCatalogTests(unittest.TestCase):
    def test_byte_identity_merges_origins_but_keeps_pdf_pages_and_distinct_videos(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name,data in [('a.mp4',b'same'),('b.mp4',b'same'),('c.mp4',b'different')]:
                (root/name).write_bytes(data)
            sources=[{'id':name,'type':'video','file':str(root/(name+'.mp4')),'url':'https://example.org/'+name}
                     for name in ('a','b','c')]
            sources += [{'id':'p1','type':'page','file':str(root/'a.mp4'),'page':1},
                        {'id':'p2','type':'page','file':str(root/'a.mp4'),'page':2}]
            images=[('a video sheet',root/'a.jpg'),('b video sheet',root/'b.jpg'),('p2',root/'p2.png')]
            self.assertEqual(deduplicate_new_sources(sources,images),{'b':'a'})
            self.assertEqual([s['id'] for s in sources],['a','c','p1','p2'])
            self.assertEqual([i[0] for i in images],['a video sheet','p2'])
            self.assertEqual(len(sources[0]['origins']),2)
            self.assertTrue((root/'b.mp4').is_file())
            self.assertEqual(coverage_summary([],sources)['independent_videos'],2)

    def reader(self,root):
        (root/'analysis').mkdir();(root/'assets').mkdir()
        return Research(root,{},None)

    def test_author_copy_of_uploaded_video_is_not_sampled_or_counted_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);r=self.reader(root);uploaded=root/'assets/input.mp4';uploaded.write_bytes(b'same video')
            sources=[{'id':'s1','name':'upload','type':'video','file':str(uploaded)}]
            r.add_media('https://example.org/demo.mp4','https://example.org/')
            with patch.object(r,'read',return_value=(b'same video','https://example.org/demo.mp4','video/mp4')), \
                 patch('paper_video.inspect_video') as inspect:
                r.download_media([],sources)
            inspect.assert_not_called()
            self.assertEqual(len(sources),1)
            self.assertEqual(r.media_inventory[0]['status'],'duplicate')
            self.assertEqual(r.media_inventory[0]['source_id'],'s1')
            self.assertEqual(r.media_coverage['independent_videos'],1)
            self.assertEqual(sources[0]['origins'][-1]['url'],'https://example.org/demo.mp4')
            self.assertEqual(r.media_inventory[0]['budget_charge_bytes'],len(b'same video'))

    def test_hosted_download_becomes_real_source_and_alias_link_uses_one_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);r=self.reader(root);sources=[]
            for url in ('https://www.youtube-nocookie.com/embed/abcdefghijk','https://youtu.be/abcdefghijk'):
                r.add_media(url,'https://example.org/',embedded_page=True)
            def download(url,target,**kwargs):
                target.write_bytes(b'video payload')
                return {'status':'downloaded','file':str(target),'budget_charge_bytes':100,'received_bytes':100}
            with patch('studio_hosted_media.download_hosted_media',side_effect=download) as fetch, \
                 patch('paper_video.inspect_video',return_value=({'duration_seconds':8},[])):
                r.download_media([],sources)
            fetch.assert_called_once();self.assertEqual(len(sources),1)
            self.assertEqual([e['status'] for e in r.media_inventory],['downloaded_and_sampled','duplicate'])
            self.assertEqual(sources[0]['content_sha256'],file_digest(sources[0]['file']))
            self.assertEqual(list((root/'assets').glob('hosted_*')),[])
            self.assertEqual(r.media_coverage['sampled'],1)

    def test_platform_failure_is_visible_and_never_fabricates_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);r=self.reader(root);sources=[]
            r.add_media('https://youtu.be/abcdefghijk','https://example.org/',embedded_page=True)
            with patch('studio_hosted_media.download_hosted_media',return_value={
                'status':'unavailable','reason':'No supported public stream','budget_charge_bytes':321}), \
                 patch('paper_video.inspect_video') as inspect:
                r.download_media([],sources)
            inspect.assert_not_called();self.assertEqual(sources,[])
            r.save();saved=json.loads((r.folder/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['media_coverage']['unavailable'],1)
            self.assertTrue(saved['gaps']);self.assertEqual(r.media_inventory[0]['budget_charge_bytes'],321)

    def test_hosted_alias_of_existing_watch_url_never_downloads_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);r=self.reader(root);target=root/'assets/existing.mp4';target.write_bytes(b'video')
            sources=[{'id':'s1','type':'video','file':str(target),'url':'https://www.youtube.com/watch?v=abcdefghijk'}]
            r.add_media('https://www.youtube-nocookie.com/embed/abcdefghijk','https://example.org/',embedded_page=True)
            with patch('studio_hosted_media.download_hosted_media') as fetch:r.download_media([],sources)
            fetch.assert_not_called();self.assertEqual(r.media_inventory[0]['status'],'duplicate')

    def test_plan_review_receives_acquisition_limits(self):
        import copy
        import paper_video as pv
        import studio_engine as engine
        from tests.test_plan_repair import PASSED,setup_job
        with tempfile.TemporaryDirectory() as temp:
            job,request,plan,_=setup_job(temp)
            (job/'analysis/research').mkdir(parents=True,exist_ok=True)
            pv.save_json(job/'analysis/research/manifest.json',{'media_coverage':{'independent_videos':1},'gaps':['Hosted original unavailable']})
            sources=pv.load_json(job/'analysis/sources.json')
            with patch.object(engine,'call_ai',return_value=copy.deepcopy(PASSED)) as ai:
                engine.review_plan(job,plan,sources,request)
            prompt=ai.call_args.args[0]
            self.assertIn('Hosted original unavailable',prompt)
            self.assertIn('independent_videos',prompt)

    def test_redirect_to_existing_url_with_new_bytes_is_new_material(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);r=self.reader(root);original=root/'assets/original.mp4'
            original.write_bytes(b'original video bytes')
            final='https://example.org/media.mp4';requested='https://mirror.example/new.mp4'
            sources=[{'id':'v1','type':'video','file':str(original),'url':final}]
            r.add_media(requested,'https://mirror.example/project')
            with patch.object(r,'read',return_value=(b'different video bytes',final,'video/mp4')), \
                 patch('paper_video.inspect_video',return_value=({'duration_seconds':8},[])) as inspect:
                r.download_media([],sources)
            inspect.assert_called_once()
            self.assertEqual(len(sources),2)
            self.assertNotEqual(sources[0]['content_sha256'],sources[1]['content_sha256'])
            self.assertEqual(r.media_inventory[0]['status'],'downloaded_and_sampled')
            self.assertEqual(r.media_coverage['independent_videos'],2)
            self.assertEqual(original.read_bytes(),b'original video bytes')

    def test_existing_url_and_hosted_alias_retain_new_project_origin(self):
        cases=[('https://example.org/demo.mp4','https://example.org/demo.mp4',False),
               ('https://www.youtube.com/watch?v=abcdefghijk','https://youtu.be/abcdefghijk',True)]
        for existing,candidate,embedded in cases:
            with self.subTest(candidate=candidate),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);r=self.reader(root);original=root/'assets/original.mp4'
                original.write_bytes(b'known video')
                sources=[{'id':'v1','type':'video','file':str(original),'url':existing,
                          'parent_url':'https://first.example/project'}]
                parent='https://second.example/project'
                r.add_media(candidate,parent,embedded_page=embedded,label='Different explanation')
                with patch.object(r,'read') as read,patch('studio_hosted_media.download_hosted_media') as download:
                    r.download_media([],sources)
                read.assert_not_called();download.assert_not_called()
                self.assertEqual(r.media_inventory[0]['status'],'duplicate')
                origin=sources[0]['origins'][-1]
                self.assertEqual(origin['parent_url'],parent)
                self.assertEqual(origin['requested_url'],candidate)
                self.assertEqual(origin['media_context']['label'],'Different explanation')
                self.assertEqual(sources[0]['origins'][0]['parent_url'],'https://first.example/project')

    def test_alias_after_hosted_attempt_preserves_origin_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);r=self.reader(root);sources=[]
            first='https://www.youtube-nocookie.com/embed/abcdefghijk'
            second='https://youtu.be/abcdefghijk'
            r.add_media(first,'https://first.example/project',embedded_page=True)
            r.add_media(second,'https://second.example/project',embedded_page=True,label='Other project context')
            def download(url,target,**kwargs):
                target.write_bytes(b'video payload')
                return {'status':'downloaded','file':str(target),'budget_charge_bytes':100}
            with patch('studio_hosted_media.download_hosted_media',side_effect=download) as fetch, \
                 patch('paper_video.inspect_video',return_value=({'duration_seconds':8},[])):
                r.download_media([],sources)
            fetch.assert_called_once()
            self.assertEqual(len(sources),1)
            self.assertEqual(sources[0]['origins'][-1]['parent_url'],'https://second.example/project')
            self.assertEqual(sources[0]['origins'][-1]['requested_url'],second)
            self.assertEqual(list((root/'assets').glob('hosted_*')),[])

    def test_unknown_or_malformed_hash_never_counts_as_verified_identity(self):
        for value in (None,'','not a hash','g'*64,42,True,{'hash':'legacy'},['a'*64]):
            with self.subTest(value=value):
                result=coverage_summary([],[{'id':'valid','type':'video','content_sha256':'a'*64},
                                            {'id':'unknown','type':'video','content_sha256':value}])
                self.assertIsNone(result['independent_videos'])
                self.assertEqual(result['unverified_video_ids'],['unknown'])
                self.assertEqual(result['available_videos'],2)
        result=coverage_summary([],[{'id':'a','type':'video','content_sha256':'a'*64},
                                    {'id':'b','type':'video','content_sha256':'A'*64}])
        self.assertEqual(result['independent_videos'],1)

    def test_known_url_is_merged_even_when_new_download_budget_is_full(self):
        for video_count,image_count in ((16,0),(1,65)):
            with self.subTest(video_count=video_count,image_count=image_count),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);r=self.reader(root);sources=[]
                for index in range(video_count):
                    path=root/'assets'/f'{index}.mp4';path.write_bytes(f'video {index}'.encode())
                    sources.append({'id':f'v{index}','type':'video','file':str(path),
                                    'url':f'https://example.org/{index}.mp4'})
                images=[(f'preview{index}',root/f'{index}.png') for index in range(image_count)]
                r.add_media('https://example.org/0.mp4','https://other.example/project')
                with patch.object(r,'read') as read:
                    r.download_media(images,sources)
                read.assert_not_called()
                self.assertEqual(r.media_inventory[0]['status'],'duplicate')
                self.assertEqual(r.media_coverage['duplicates'],1)
                self.assertEqual(r.media_coverage['not_reviewed'],0)
                self.assertEqual(sources[0]['origins'][-1]['parent_url'],'https://other.example/project')


if __name__=='__main__':unittest.main()
