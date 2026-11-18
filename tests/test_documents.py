import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from studio_documents import inspect_document


def make_pdf(path, count):
    with pymupdf.open() as document:
        for number in range(1, count + 1):
            page = document.new_page(width=320, height=420)
            page.insert_text((20, 35), f'Document page {number}', fontsize=12)
            page.insert_text((20, 70), f'Unique content {number}: complete source text.', fontsize=9)
            if number == 2:
                page.insert_text((20, 100), 'Table of Contents', fontsize=11)
            if number % 7 == 0:
                page.insert_text((20, 130), f'Figure {number}: source diagram.', fontsize=10)
        document.set_toc([[1, 'Opening', 1], [1, 'Final section', count]])
        document.save(path)


class DocumentTests(unittest.TestCase):
    def test_long_document_preserves_all_text_and_samples_full_extent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve(); source = root / 'long.pdf'; analysis = root / 'analysis'
            make_pdf(source, 135)
            pages, images = inspect_document(source, analysis)
            self.assertEqual(len(pages), 135)
            self.assertEqual(len(images), 24)
            with pymupdf.open(source) as original:
                for saved, page in zip(pages, original):
                    self.assertEqual(saved['text'], page.get_text('text', sort=False))
            selected = [p['page'] for p in pages if p['file']]
            self.assertEqual(selected, sorted(set(selected)))
            self.assertIn(1, selected); self.assertIn(2, selected); self.assertIn(135, selected)
            self.assertTrue(any(100 <= n < 135 for n in selected))
            self.assertEqual(images, [analysis / p['file'] for p in pages if p['file']])
            self.assertTrue(all(p.is_absolute() and p.is_file() for p in images))
            self.assertTrue(any(p['file'] is None for p in pages))
            manifest = json.loads((analysis / 'document_manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['text_pages_processed'], 135)
            self.assertEqual(manifest['character_count'], sum(len(p['text']) for p in pages))
            self.assertEqual(manifest['image_sample_pages'], selected)
            self.assertEqual(manifest['outline'][-1]['page'], 135)
            self.assertEqual(json.loads((analysis / 'paper_pages.json').read_text(encoding='utf-8')), pages)

    def test_short_document_renders_every_page_and_40_pages_stays_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for count in (4, 24, 40):
                source = root / f'{count}.pdf'; make_pdf(source, count)
                pages, images = inspect_document(source, root / str(count))
                self.assertEqual(len(pages), count)
                self.assertEqual(len(images), min(count, 24))
                self.assertIsNotNone(pages[-1]['file'])

    def test_budgets_fail_before_rendering(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'input.pdf'; make_pdf(source, 3)
            for kwargs, message in [({'max_pages': 2}, 'page count'),
                                    ({'max_characters': 10}, 'max_characters')]:
                with patch.object(pymupdf.Page, 'get_pixmap') as render:
                    with self.assertRaisesRegex(ValueError, message):
                        inspect_document(source, root / 'analysis', **kwargs)
                    render.assert_not_called()
                self.assertFalse((root / 'analysis').exists())

    def test_scan_only_reports_no_ocr_before_rendering(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'scan.pdf'
            with pymupdf.open() as document:
                page = document.new_page(); page.draw_rect(pymupdf.Rect(20, 20, 100, 100))
                document.save(source)
            with patch.object(pymupdf.Page, 'get_pixmap') as render:
                with self.assertRaisesRegex(ValueError, 'OCR'):
                    inspect_document(source, root / 'analysis')
                render.assert_not_called()

    def test_text_only_mode_and_invalid_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / 'input.pdf'; make_pdf(source, 2)
            pages, images = inspect_document(source, root / 'analysis', max_previews=0)
            self.assertEqual(images, [])
            self.assertTrue(all(p['file'] is None for p in pages))
            with self.assertRaises(ValueError):
                inspect_document(source, root / 'bad', max_previews=-1)


if __name__ == '__main__':
    unittest.main()
