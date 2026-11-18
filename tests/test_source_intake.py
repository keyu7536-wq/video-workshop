import shutil
import io
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pymupdf
import studio_engine as engine


class SourceIntakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.job = self.root/'job'
        for name in ('assets', 'analysis'):
            (self.job/name).mkdir(parents=True)
        self.pdf = self.root/'source.pdf'
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 60), '\n'.join([
                'An actual paper body explains a concrete task and its evidence.',
                'The previous method has a specific computational limitation.',
                'This method changes the representation to reduce repeated work.',
                'The evaluation compares the methods under matched conditions.',
                'The limitations describe untested settings and remaining questions.',
            ]))
            # A valid large PDF proves intake uses the document downloader rather
            # than the small research-webpage fetch budget.
            document.set_metadata({'subject':'x'*(3*1024*1024+128)})
            document.save(self.pdf)

    def tearDown(self):
        self.temporary.cleanup()

    def download(self, url, target, kind):
        self.assertEqual(kind, 'pdf')
        shutil.copy2(self.pdf, target)
        return url

    def test_url_only_article_reads_real_pdf_body_and_preserves_provenance(self):
        url = 'https://example.org/paper.pdf'
        self.assertGreater(self.pdf.stat().st_size, 3*1024*1024)
        with patch('studio_research.public_url', side_effect=lambda value:value), \
             patch('paper_video.acquire', side_effect=self.download) as download, \
             patch('studio_research.fetch', side_effect=AssertionError('Wrong fetch route')), \
             patch('studio_engine.call_ai') as ai:
            sources, images = engine.collect(self.job, {'article':url, 'links':[]})
        download.assert_called_once()
        ai.assert_not_called()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]['type'], 'page')
        self.assertIn('An actual paper body', sources[0]['text'])
        self.assertNotEqual(sources[0]['text'].strip(), url)
        self.assertEqual(sources[0]['url'], url)
        self.assertEqual(sources[0]['input_field'], 'article')
        self.assertNotEqual(sources[0]['id'], 'article')
        self.assertTrue(images[0][1].is_file())

    def test_article_and_explicit_links_are_deduplicated(self):
        first, second = 'https://example.org/first.pdf', 'https://example.org/second.pdf'
        request = {'article':f'  {first}\n\n{second}\n{first}  ', 'links':[first, first]}
        with patch('studio_research.public_url', side_effect=lambda value:value), \
             patch('paper_video.acquire', side_effect=self.download) as download:
            sources, _ = engine.collect(self.job, request)
        self.assertEqual(download.call_count, 2)
        self.assertEqual([s['url'] for s in sources], [first, second])
        self.assertEqual([s['input_field'] for s in sources], ['links', 'article'])

    def test_mixed_article_prose_is_not_treated_as_a_download_list(self):
        text = '这篇文章讨论一个问题。\nhttps://example.org/reference.pdf\n这是正文中的引用。'
        with patch('studio_engine.acquire_link') as acquire:
            sources, _ = engine.collect(self.job, {'article':text})
        acquire.assert_not_called()
        self.assertEqual(sources[0]['id'], 'article')
        self.assertEqual(sources[0]['text'], text)

    def test_protected_script_urls_are_never_reinterpreted(self):
        script = 'https://example.org/paper.pdf'
        with patch('studio_engine.acquire_link') as acquire:
            sources, _ = engine.collect(self.job, {'script':script})
        acquire.assert_not_called()
        self.assertEqual(sources[0]['id'], 'script')
        self.assertEqual(sources[0]['text'], script)

    def test_download_failure_names_article_input_and_never_becomes_source_text(self):
        with patch('studio_engine.acquire_link', side_effect=URLError('network unavailable')), \
             patch('studio_engine.call_ai') as ai:
            with self.assertRaisesRegex(ValueError, r'文章正文中的链接读取失败.*example.org/paper.pdf'):
                engine.collect(self.job, {'article':'https://example.org/paper.pdf'})
        ai.assert_not_called()
        self.assertFalse((self.job/'analysis/sources.json').exists())

    def test_rate_limited_source_has_actionable_message_and_does_not_invent_content(self):
        url = 'https://example.org/paper.pdf?token=not-for-errors'
        error = HTTPError(url, 429, 'Too Many Requests', {}, None)
        with patch('studio_engine.acquire_link', side_effect=error), patch('studio_engine.call_ai') as ai:
            with self.assertRaises(ValueError) as caught:
                engine.collect(self.job, {'article':url})
        self.assertIn('429', str(caught.exception))
        self.assertIn('上传', str(caught.exception))
        self.assertIn('官方', str(caught.exception))
        self.assertNotIn('not-for-errors', str(caught.exception))
        ai.assert_not_called()
        self.assertFalse((self.job/'analysis/sources.json').exists())

    def test_existing_local_file_and_explicit_link_inputs_still_work(self):
        local = self.root/'notes.txt'
        local.write_text('本地说明仍然作为普通文本资料。', encoding='utf-8')
        with patch('studio_research.public_url', side_effect=lambda value:value), \
             patch('paper_video.acquire', side_effect=self.download):
            sources, _ = engine.collect(self.job, {'files':[{'path':str(local), 'name':'notes.txt'}],
                'links':['https://example.org/paper.pdf']})
        self.assertEqual([s['type'] for s in sources], ['text', 'page'])
        self.assertNotIn('url', sources[0])
        self.assertEqual(sources[1]['input_field'], 'links')

    def test_article_url_uses_existing_public_source_guard(self):
        with patch('paper_video.acquire') as download:
            with self.assertRaisesRegex(ValueError, '文章正文中的链接读取失败.*不抓取内网或本机地址'):
                engine.collect(self.job, {'article':'http://127.0.0.1/private.pdf'})
        download.assert_not_called()

    def test_long_document_keeps_all_pages_with_sparse_correct_preview_ids(self):
        def inspect(path, folder, **budget):
            self.assertEqual(budget['max_pages'], 400)
            self.assertEqual(budget['max_previews'], 24)
            pages = [{'page':n, 'text':f'Actual evidence on page {n}.', 'file':None}
                     for n in range(1, 136)]
            previews = []
            for number in (1, 67, 135):
                relative = f'page_{number}.png'
                (folder/relative).write_bytes(b'preview fixture')
                pages[number-1]['file'] = relative
                previews.append(folder/relative)
            return pages, previews
        with patch('studio_documents.inspect_document', side_effect=inspect):
            sources, images = engine.collect(self.job, {'files':[str(self.pdf)]})
        self.assertEqual(len(sources), 135)
        self.assertEqual(sources[-1]['id'], 's1_p135')
        self.assertIn('page 135', sources[-1]['text'])
        self.assertNotIn('preview', sources[1])
        self.assertEqual([identifier for identifier, _ in images], ['s1_p1', 's1_p67', 's1_p135'])
        self.assertTrue(all(image.is_file() for _, image in images))

    def test_real_135_page_pdf_is_fully_extracted_with_bounded_previews(self):
        long_pdf = self.root/'long_document.pdf'
        with pymupdf.open() as document:
            for number in range(1, 136):
                page = document.new_page(width=200, height=200)
                page.insert_text((12, 25), f'Actual evidence on page {number}.', fontsize=8)
            document.save(long_pdf)
        sources, images = engine.collect(self.job, {'files':[str(long_pdf)]})
        self.assertEqual(len(sources), 135)
        self.assertIn('Actual evidence on page 135.', sources[-1]['text'])
        self.assertEqual(len(images), 24)
        lookup = {source['id']:source for source in sources}
        self.assertEqual(images[-1][0], 's1_p135')
        for source_id, image in images:
            self.assertEqual(Path(lookup[source_id]['preview']), image)
            self.assertTrue(image.is_file())

    def test_document_budget_accounts_for_prior_pages_and_source_text(self):
        calls = []
        def inspect(path, folder, **budget):
            calls.append(budget)
            return [{'page':n, 'text':'source text', 'file':None} for n in range(1, 136)], []
        with patch('studio_documents.inspect_document', side_effect=inspect):
            sources, _ = engine.collect(self.job, {'files':[str(self.pdf), str(self.pdf)], 'article':'ordinary article'})
        self.assertEqual(len(sources), 271)
        self.assertEqual([c['max_pages'] for c in calls], [400, 265])
        self.assertEqual(calls[0]['max_characters'], 2_000_000-len('ordinary article'))
        self.assertEqual(calls[1]['max_characters'], calls[0]['max_characters']-135*len('source text'))

    def response(self, body, content_type):
        response = io.BytesIO(body)
        response.headers = Message()
        response.headers['Content-Type'] = content_type
        return response

    def test_suffixless_pdf_over_html_budget_uses_open_response_as_document(self):
        large_pdf = self.root/'large.pdf'
        with pymupdf.open(self.pdf) as document:
            document.set_metadata({'subject':'x'*(6*1024*1024)})
            document.save(large_pdf)
        body = large_pdf.read_bytes()
        self.assertGreater(len(body), 5*1024**2)
        for content_type in ('application/pdf; charset=binary', 'application/octet-stream'):
            with self.subTest(content_type=content_type):
                response = self.response(body, content_type)
                with patch('studio_research.public_url') as guard, \
                     patch('urllib.request.build_opener') as opener, \
                     patch('paper_video.acquire', side_effect=AssertionError('Response must be reused')):
                    opener.return_value.open.return_value = response
                    sources, _ = engine.collect(self.job, {'article':'https://example.org/pdf?id=paper'})
                guard.assert_called_once_with('https://example.org/pdf?id=paper')
                opener.return_value.open.assert_called_once()
                self.assertTrue(response.closed)
                self.assertEqual(sources[0]['type'], 'page')
                self.assertIn('An actual paper body', sources[0]['text'])
                self.assertEqual(Path(sources[0]['file']).read_bytes(), body)
                self.assertEqual(sources[0]['url'], 'https://example.org/pdf?id=paper')

    def test_suffixless_pdf_over_document_limit_is_rejected_without_partial_file(self):
        from studio_research import DOCUMENT_BYTES
        class LargeResponse:
            def __init__(self):
                self.headers = Message(); self.headers['Content-Type'] = 'application/pdf'
                self.remaining = DOCUMENT_BYTES+1; self.read_count = 0; self.closed = False
            def __enter__(self):return self
            def __exit__(self, *args):self.closed = True
            def read(self, size):
                amount = min(size, self.remaining); self.remaining -= amount; self.read_count += 1
                return b'x'*amount
        response = LargeResponse()
        with patch('studio_research.public_url'), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError, '文章正文中的链接读取失败.*PDF.*100 MiB'):
                engine.collect(self.job, {'article':'https://example.org/pdf?id=large'})
        self.assertGreater(response.read_count, 2)
        self.assertTrue(response.closed)
        self.assertEqual(list((self.job/'assets').iterdir()), [])

    def test_suffixless_html_remains_an_article_with_original_size_limit(self):
        body = '<html><p>'+('正文介绍具体的问题和论据。'*20)+'</p><script>hidden</script></html>'
        response = self.response(body.encode('utf-8'), 'text/html; charset=utf-8')
        with patch('studio_research.public_url'), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            target = engine.acquire_link('https://example.org/article?id=1', self.job/'assets', 1)
        self.assertEqual(target.suffix, '.txt')
        self.assertIn('正文介绍具体的问题和论据。', target.read_text(encoding='utf-8'))
        self.assertNotIn('hidden', target.read_text(encoding='utf-8'))
        response = self.response(b'<html>'+b'x'*(5*1024**2), 'text/html')
        with patch('studio_research.public_url'), patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(ValueError, '文章网页过大'):
                engine.acquire_link('https://example.org/article?id=2', self.job/'assets', 2)


if __name__ == '__main__':
    unittest.main()
