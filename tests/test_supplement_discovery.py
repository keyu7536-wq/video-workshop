"""Explicit author documents remain discoverable without special link labels."""
import unittest
from unittest.mock import patch

from studio_media import MediaHTML, readme_media


class SupplementDiscoveryTests(unittest.TestCase):
    def test_html_plain_pdf_mime_and_role_are_separate_from_media(self):
        page = MediaHTML('https://author.example/project/')
        page.feed('''<a href="paper.pdf">Paper</a><a href="appendix.pdf">PDF</a>
          <a href="download?id=2" type="application/pdf">Download</a>
          <a href="results.PDF?version=2#table">Results</a>
          <iframe src="appendix.pdf#page=3"></iframe>
          <video src="demo.mp4"></video>''')
        self.assertEqual(len(page.linked_pages), 4)
        roles = {item['url']: item['document_role'] for item in page.linked_pages}
        self.assertEqual(roles['https://author.example/project/paper.pdf'], 'paper')
        self.assertEqual(roles['https://author.example/project/appendix.pdf'], 'supplement')
        self.assertEqual(roles['https://author.example/project/download?id=2'], 'unspecified')
        self.assertTrue(all(item['explicit_author_link'] for item in page.linked_pages))
        self.assertEqual([i['url'] for i in page.media_items], ['https://author.example/project/demo.mp4'])

    def test_markdown_inline_reference_and_plain_urls_find_documents(self):
        page = readme_media('''[Supplementary](appendix.pdf)
        [PDF][paper]

[paper]: https://publisher.example/paper.pdf

https://author.example/results.PDF
[Additional demos](supplement.html)
[Method](method.html)
![Figure](figure.png)
[Talk](https://youtube.com/watch?v=abc)
''', 'https://raw.githubusercontent.com/author/project/'+'a'*40+'/README.md')
        self.assertEqual(len(page.linked_pages), 4)
        self.assertEqual(sum(x['kind']=='supplement_document' for x in page.linked_pages), 3)
        self.assertTrue(any(x['kind']=='supplement_page' for x in page.linked_pages))
        self.assertFalse(any('method.html' in x['url'] for x in page.linked_pages))
        self.assertTrue(any(x['media_type_hint']=='image' for x in page.media_items))
        self.assertTrue(any(x['embedded_page'] for x in page.media_items))

    def test_document_discovery_is_bounded_deduplicated_and_only_web_links(self):
        page = MediaHTML('https://author.example/')
        with patch('studio_media.MAX_DISCOVERED', 2):
            page.feed('''<a href="a.pdf">PDF</a><a href="a.pdf#x">Again</a>
              <a href="b.pdf">PDF</a><a href="c.pdf">PDF</a>
              <a href="javascript:alert(1)">Supplementary</a>
              <a href="data:application/pdf;base64,abc">Supplementary</a>
              <a href="https://user:password@author.example/a.pdf">Supplementary</a>''')
        self.assertEqual(len(page.linked_pages), 2)
        self.assertEqual(page.omitted, 1)

    def test_pdf_embed_and_missing_type_attribute_do_not_crash(self):
        page = MediaHTML('https://author.example/')
        page.feed('<embed src="appendix.pdf" type><a href="other.pdf" type>PDF</a>')
        self.assertEqual(len(page.linked_pages), 2)
        self.assertEqual(page.media_items, [])


if __name__ == '__main__':
    unittest.main()
