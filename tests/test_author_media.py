"""Author media remains attributable, bounded and genuinely decodable."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from email.message import Message
import ssl

from PIL import Image
import studio_research as research
from studio_media import readme_media, response_format


def png():
    stream=io.BytesIO();Image.new('RGB',(640,360),'red').save(stream,'PNG')
    return stream.getvalue()


class AuthorMediaTests(unittest.TestCase):
    def reader(self,folder,ai=None):
        job=Path(folder);(job/'analysis').mkdir();(job/'assets').mkdir()
        return research.Research(job,{},ai)

    def test_page_preserves_lazy_source_type_title_and_section(self):
        page=research.Page('https://author.example/project/')
        page.feed('''<h3>Failure comparisons</h3><video title="Policy recovers">
            <source data-src="/download?id=23" type="video/mp4"></video>
            <img data-original="method.png" alt="Model architecture">
            <iframe src="https://www.youtube-nocookie.com/embed/abc" title="Author talk"></iframe>''')
        by_url={p['url']:p for p in page.media_items}
        video=by_url['https://author.example/download?id=23']
        self.assertEqual(video['media_type_hint'],'video')
        self.assertEqual(video['label'],'Policy recovers')
        self.assertEqual(video['context'],'Failure comparisons')
        self.assertTrue(by_url['https://www.youtube-nocookie.com/embed/abc']['embedded_page'])

    def test_readme_html_reference_and_bare_attachment_are_discovered(self):
        raw='https://raw.githubusercontent.com/author/project/'+'a'*40+'/README.md'
        text='''<video><source src="assets/rollout" type="video/mp4"></video>
            <img src="assets/architecture.png" alt="Method">
            ![Results][figure]
[figure]: assets/result.jpg "Result comparison"
            https://github.com/user-attachments/assets/abc-123
            [Demo](https://github.com/author/project/blob/abcd/clip.mp4)
            [Talk](https://youtu.be/author-talk)
            <script>do_not_execute()</script>'''
        page=readme_media(text,raw);urls={p['url'] for p in page.media_items}
        self.assertIn(raw.rsplit('/',1)[0]+'/assets/rollout',urls)
        self.assertIn(raw.rsplit('/',1)[0]+'/assets/result.jpg',urls)
        self.assertIn('https://github.com/user-attachments/assets/abc-123',urls)
        self.assertIn('https://raw.githubusercontent.com/author/project/abcd/clip.mp4',urls)
        self.assertTrue(next(p for p in page.media_items if p['url']=='https://youtu.be/author-talk')['embedded_page'])
        self.assertTrue(all(not u.startswith(('javascript:','data:')) for u in urls))

    def test_data_srcset_is_not_split_into_an_invented_relative_request(self):
        page=research.Page('https://author.example/')
        page.feed('<img srcset="data:image/png;base64,iVBORw0KGgoAAAANSUhEUg 1x">')
        self.assertEqual(page.media_items,[])

    def test_suffixless_actual_image_enters_sources_with_context_and_digest(self):
        url='https://author.example/image?id=1';body=png()
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder);page=research.Page('https://author.example/')
            page.feed('<h2>Architecture</h2><img src="image?id=1" alt="World model">')
            reader.ingest_media(page,page.url);sources=[];images=[]
            with patch.object(reader,'read',return_value=(body,url,'image/png')):
                reader.download_media(images,sources)
            self.assertEqual(len(sources),1)
            self.assertEqual(sources[0]['type'],'image')
            self.assertEqual(Path(sources[0]['file']).read_bytes(),body)
            self.assertEqual(sources[0]['media_context']['context'],'Architecture')
            self.assertEqual(reader.media_inventory[0]['sha256'],hashlib.sha256(body).hexdigest())
            self.assertEqual(reader.media_inventory[0]['status'],'downloaded_and_sampled')

    def test_suffixless_declared_video_uses_response_mime_and_video_inspector(self):
        url='https://author.example/stream?asset=42'
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder);reader.add_media(url,'https://author.example/',media_type_hint='video')
            with patch.object(reader,'read',return_value=(b'video bytes',url,'video/mp4')) as read, \
                 patch('paper_video.inspect_video',return_value=({'duration_seconds':3},[])) as inspect:
                sources=[];reader.download_media([],sources)
            inspect.assert_called_once();self.assertEqual(sources[0]['type'],'video')
            self.assertTrue(sources[0]['file'].endswith('.mp4'))
            self.assertEqual(read.call_args.args[2],120*1024**2)

    def test_html_disguised_as_mp4_and_mismatched_type_never_enter_sources(self):
        for mime,data in [('text/html',b'<html>Access denied</html>'),
                          ('video/mp4',b'<!DOCTYPE html><html>Error</html>'),
                          ('image/png',png())]:
            with self.subTest(mime=mime,data=data[:10]),tempfile.TemporaryDirectory() as folder:
                reader=self.reader(folder);url='https://author.example/demo.mp4'
                reader.media=[(url,'https://author.example/')];sources=[]
                with patch.object(reader,'read',return_value=(data,url,mime)),patch('paper_video.inspect_video') as inspect:
                    reader.download_media([],sources)
                self.assertEqual(sources,[]);inspect.assert_not_called()
                self.assertEqual(reader.media_inventory[0]['status'],'inspection_failed')
                self.assertTrue(reader.gaps)

    def test_octet_stream_requires_supported_magic(self):
        self.assertEqual(response_format('https://author.example/x','application/octet-stream',png()),('image','.png'))
        with self.assertRaises(ValueError):response_format('https://author.example/x.mp4','application/octet-stream',b'not media')

    def test_external_players_are_inventory_gaps_not_downloads(self):
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder);page=research.Page('https://author.example/')
            page.feed('<iframe src="https://www.youtube-nocookie.com/embed/abc"></iframe><a href="https://vimeo.com/123">Talk</a>')
            reader.ingest_media(page,page.url)
            with patch.object(reader,'read') as read:
                reader.linked_media(page,page.url);reader.download_media([],[])
            read.assert_not_called()
            self.assertEqual(len(reader.media_inventory),2)
            self.assertTrue(all(r['status']=='not_reviewed' for r in reader.media_inventory))
            self.assertIn('未下载',reader.media_inventory[0]['reason'])
            reader.save();saved=json.loads((reader.folder/'media_inventory.json').read_text(encoding='utf-8'))
            self.assertTrue(saved['linked_pages'])

    def test_supplement_and_same_origin_embed_one_hop_three_page_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder);page=research.Page('https://author.example/project/')
            page.feed(''.join(f'<a href="extra{i}.html">Supplementary videos</a>' for i in range(4)))
            def read(url,*args):
                return b'<h3>Failure test</h3><video src="failure.mp4"></video><iframe src="nested.html"></iframe>',url,'text/html'
            with patch.object(reader,'read',side_effect=read) as network:
                reader.linked_media(page,page.url)
            self.assertEqual(network.call_count,3)
            self.assertIn(('https://author.example/project/failure.mp4','https://author.example/project/extra0.html'),reader.media)
            self.assertTrue(any(r.get('reason')=='补充媒体页面深度上限为1' for r in reader.linked_media_pages))
            self.assertTrue(any('3个' in r.get('reason','') for r in reader.linked_media_pages))

    def test_private_embedded_page_is_not_read_and_fetch_guard_is_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder);page=research.Page('https://author.example/')
            page.feed('<iframe src="http://127.0.0.1/private"></iframe>')
            with patch.object(reader,'read') as read:reader.linked_media(page,page.url)
            read.assert_not_called()
        with self.assertRaises(ValueError):research.public_url('http://127.0.0.1/private')

    def test_redirect_to_foreign_html_not_parsed_as_author_supplement(self):
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder);page=research.Page('https://author.example/')
            page.feed('<iframe src="player.html"></iframe>')
            with patch.object(reader,'read',return_value=(b'<video src="fake.mp4"></video>','https://other.example/','text/html')):
                reader.linked_media(page,page.url)
            self.assertEqual(reader.media,[])
            self.assertEqual(reader.linked_media_pages[0]['status'],'not_parsed')

    def test_total_bytes_and_duplicate_resolved_media_are_bounded(self):
        body=png();urls=['https://author.example/a','https://author.example/b']
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder)
            for url in urls:reader.add_media(url,'https://author.example/',media_type_hint='image')
            with patch.object(research,'MEDIA_TOTAL_BYTES',len(body)), \
                 patch.object(reader,'read',return_value=(body,urls[0],'image/png')) as read:
                sources=[];reader.download_media([],sources)
            self.assertEqual(read.call_count,1)
            self.assertEqual(reader.media_inventory[1]['status'],'not_reviewed')
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder)
            for url in urls:reader.add_media(url,'https://author.example/',media_type_hint='image')
            with patch.object(reader,'read',return_value=(body,'https://cdn.example/same.png','image/png')):
                sources=[];reader.download_media([],sources)
            self.assertEqual(len(sources),1)
            self.assertEqual(reader.media_inventory[1]['status'],'duplicate')
            self.assertEqual(reader.media_inventory[1]['source_id'],sources[0]['id'])

    def test_failed_unmetered_requests_charge_allowance_and_stop_total_budget(self):
        calls=[]
        def failing(url,limit,**kwargs):
            calls.append(limit);raise ValueError('oversized response after partial read')
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder)
            for i in range(3):reader.add_media(f'https://author.example/{i}.mp4','https://author.example/')
            with patch.object(research,'MEDIA_TOTAL_BYTES',1024),patch.object(research,'fetch',side_effect=failing):
                reader.download_media([],[])
            self.assertEqual(calls,[1024])
            self.assertEqual(reader.media_remaining_bytes,0)
            self.assertEqual(reader.records[0]['byte_accounting'],'conservative requested limit')
            self.assertEqual(reader.media_inventory[1]['status'],'not_reviewed')

    def test_measured_zero_byte_failure_leaves_budget_for_valid_next_media(self):
        body=png();calls=[]
        def fetched(url,limit,**kwargs):
            calls.append(url)
            if url.endswith('/blocked'):
                error=ValueError('Content-Length rejected before reading body')
                error.received_bytes=0
                raise error
            return body,url,'image/png'
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder)
            for suffix in ['blocked','valid']:reader.add_media('https://author.example/'+suffix,'https://author.example/',media_type_hint='image')
            with patch.object(research,'MEDIA_TOTAL_BYTES',len(body)+32),patch.object(research,'fetch',side_effect=fetched):
                sources=[];reader.download_media([],sources)
            self.assertEqual(len(calls),2)
            self.assertEqual(len(sources),1)
            self.assertEqual(reader.media_inventory[0]['budget_charge_bytes'],0)
            self.assertIn('measured',reader.media_inventory[0]['budget_accounting'])
            self.assertEqual(reader.media_remaining_bytes,32)

    def test_fetch_counts_failed_attempt_bytes_and_cannot_retry_past_transfer_budget(self):
        class Response:
            def __init__(self,chunks):
                self.chunks=iter(chunks);self.headers=Message();self.headers['Content-Type']='video/mp4'
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def geturl(self):return 'https://author.example/clip'
            def read1(self,size):
                value=next(self.chunks,b'')
                if isinstance(value,Exception):raise value
                self.asserted_size=size
                return value[:size]
        first=Response([b'123',ssl.SSLEOFError('transport ended')])
        second=Response([b'456',b''])
        with patch.object(research,'public_url',side_effect=lambda u:u),patch.object(research,'build_opener') as builder:
            builder.return_value.open.side_effect=[first,second]
            with self.assertRaisesRegex(ValueError,'预算') as raised:
                research.fetch('https://author.example/clip',100,transfer_budget=6)
        self.assertEqual(raised.exception.received_bytes,6)
        self.assertEqual(builder.return_value.open.call_count,2)
        first=Response([b'123',ssl.SSLEOFError('transport ended')]);second=Response([b'456',b''])
        with patch.object(research,'public_url',side_effect=lambda u:u),patch.object(research,'build_opener') as builder:
            builder.return_value.open.side_effect=[first,second]
            value=research.fetch('https://author.example/clip',100,transfer_budget=20)
        self.assertEqual(value[0],b'456')
        self.assertEqual(value.received_bytes,6)

    def test_fragment_variants_are_one_request_with_original_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder)
            reader.add_media('https://author.example/image#first','https://author.example/a',media_type_hint='image')
            reader.add_media('https://author.example/image#second','https://author.example/b',media_type_hint='image')
            with patch.object(reader,'read',return_value=(png(),'https://cdn.example/final.png','image/png')) as read:
                sources=[];reader.download_media([],sources)
            self.assertEqual(read.call_count,1)
            self.assertEqual(len(sources),1)
            self.assertEqual(len(reader.media_inventory[0]['original_urls']),2)
            self.assertEqual(len(reader.media_inventory[0]['discovered_from']),2)

    def test_api_repository_readme_html_media_pins_relative_path_to_commit(self):
        commit='2'*40;tree='1'*40
        with tempfile.TemporaryDirectory() as folder:
            reader=self.reader(folder,lambda *args:{'paths':[]})
            def read(url,*args):
                if '/git/trees/' in url:value={'sha':tree,'tree':[{'path':'README.md','type':'blob','size':100}]}
                elif '/commits/' in url:value={'sha':commit,'commit':{'tree':{'sha':tree}}}
                elif 'api.github.com' in url:value={'default_branch':'main','license':None}
                else:return b'<video src="assets/demo.mp4"></video> Project implementation source.',url,'text/plain'
                return json.dumps(value).encode(),url,'application/json'
            with patch.object(reader,'read',side_effect=read):reader.repo('https://github.com/author/project')
            self.assertIn('https://raw.githubusercontent.com/author/project/'+commit+'/assets/demo.mp4',[u for u,_ in reader.media])


if __name__=='__main__':unittest.main()
