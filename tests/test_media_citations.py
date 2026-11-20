"""Explicit bibliography links stay discoverable without consuming author-document budgets."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from studio_media import MediaHTML, readme_media
from studio_research import Research
from tests.test_research_documents import pdf_bytes


class MediaCitationTests(unittest.TestCase):
    def reader(self, folder):
        root=Path(folder)
        (root/'analysis').mkdir();(root/'assets').mkdir()
        return Research(root,{},None)

    def test_explicit_labels_and_reference_headings_skip_but_retain_inventory(self):
        cases=[('<a href="cited.pdf">Reference</a>','Reference'),
               ('<a href="cited.pdf">References</a>','References'),
               ('<a href="cited.pdf">参考文献</a>','参考文献'),
               ('<h2>Related work</h2><a href="cited.pdf">A different paper</a>','A different paper'),
               ('<h2>参考文献</h2><a href="cited.pdf">Earlier method</a>','Earlier method')]
        for html,label in cases:
            with self.subTest(html=html),tempfile.TemporaryDirectory() as temp:
                page=MediaHTML('https://author.example/project');page.feed(html)
                self.assertEqual(page.linked_pages[0]['document_role'],'citation')
                reader=self.reader(temp)
                with patch.object(reader,'read') as read:
                    reader.linked_media(page,page.url)
                read.assert_not_called()
                self.assertEqual(reader.seen_media_pages,set())
                self.assertEqual(reader.linked_media_pages[0]['status'],'not_read')
                self.assertEqual(reader.linked_media_pages[0]['label'],label)
                self.assertIn('参考文献',reader.linked_media_pages[0]['reason'])

    def test_citations_do_not_consume_budget_and_plain_download_pdf_is_read(self):
        page=MediaHTML('https://author.example/project')
        page.feed(''.join(f'<a href="reference{index}.pdf">Reference</a>' for index in range(4))+
                  '<h2>Downloads</h2><a href="full.pdf">Download PDF</a>')
        self.assertEqual(page.linked_pages[-1]['document_role'],'unspecified')
        with tempfile.TemporaryDirectory() as temp:
            reader=self.reader(temp)
            with patch.object(reader,'read',return_value=(pdf_bytes(1),'https://author.example/full.pdf','application/pdf')) as read:
                reader.linked_media(page,page.url)
            read.assert_called_once()
            self.assertEqual(read.call_args.args[0],'https://author.example/full.pdf')
            self.assertEqual(len(reader.seen_media_pages),1)
            self.assertEqual(len(reader.sources),1)
            self.assertEqual(reader.sources[0]['type'],'page')
            self.assertEqual(reader.linked_media_pages[-1]['status'],'document_read')
            self.assertTrue(all(item['status']=='not_read' for item in reader.linked_media_pages[:-1]))

    def test_readme_plain_pdf_and_author_paper_appendix_remain_eligible(self):
        page=readme_media('[Download](paper.pdf)\n[Paper](body.pdf)\n[Appendix](appendix.pdf)\n[Reference](earlier.pdf)',
                          'https://raw.githubusercontent.com/author/project/main/README.md')
        roles={item['url'].split('/')[-1]:item['document_role'] for item in page.linked_pages}
        self.assertEqual(roles,{'paper.pdf':'unspecified','body.pdf':'paper',
                                'appendix.pdf':'supplement','earlier.pdf':'citation'})


if __name__=='__main__':unittest.main()
