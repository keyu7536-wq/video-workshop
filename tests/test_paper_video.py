import copy
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paper_video as pv


def example_plan():
    figure = {'id': 'fig1', 'page': 1, 'bbox': [.1, .2, .9, .8], 'label': 'Fig. 1'}
    evidence = [{'page': 1, 'quote': 'Our method uses simulated training data.'}]
    image = {'kind': 'figure', 'figure_id': 'fig1', 'in': 0, 'duration': 4,
             'label': '方法图', 'cue': '', 'visual_type': 'paper'}
    video = {'kind': 'video', 'figure_id': '', 'in': 0, 'duration': 4,
             'label': '仿真演示', 'cue': '', 'visual_type': 'simulation'}
    return {'title': '测试研究', 'paper_title': 'Demo Paper', 'summary': '测试摘要', 'limitations': ['只在仿真中测试'],
            'figures': [figure], 'blocks': [
                {'id': '01', 'chapter': '问题', 'narration': '这项研究尝试用仿真数据训练模型，减少采集训练数据的工作。', 'evidence': copy.deepcopy(evidence), 'shots': [video]},
                {'id': '02', 'chapter': '方法', 'narration': '首先在模拟环境中构建训练样本，然后训练模型完成任务。', 'evidence': copy.deepcopy(evidence), 'shots': [copy.deepcopy(image)]},
                {'id': '03', 'chapter': '限制', 'narration': '不过，目前只在仿真环境测试，不能直接说明真实环境中的效果。', 'evidence': copy.deepcopy(evidence), 'shots': [copy.deepcopy(image)]},
            ]}


class PlanValidationTests(unittest.TestCase):
    def setUp(self):
        self.plan = example_plan()
        self.pages = [{'page': 1, 'text': 'Demo Paper\nOur method uses simulated training data.\nOnly simulation is evaluated.'}]
        self.info = {'duration_seconds': 8, 'sample_times': [0, 2, 4, 6]}

    def check(self):
        pv.validate_plan(self.plan, self.pages, self.info)

    def test_valid_plan(self):
        self.check()

    def test_rejects_fabricated_evidence(self):
        self.plan['blocks'][1]['evidence'][0]['quote'] = 'Our robot succeeds on all real world tasks.'
        with self.assertRaisesRegex(ValueError, '依据不匹配'):
            self.check()

    def test_rejects_out_of_range_pages(self):
        self.plan['figures'][0]['page'] = 20
        with self.assertRaisesRegex(ValueError, '页码越界'):
            self.check()

    def test_rejects_reversed_crop(self):
        self.plan['figures'][0]['bbox'] = [.9, .1, .2, .8]
        with self.assertRaisesRegex(ValueError, '裁切范围'):
            self.check()

    def test_rejects_video_past_end(self):
        self.plan['blocks'][0]['shots'][0]['in'] = 6
        with self.assertRaisesRegex(ValueError, '超出原片'):
            self.check()

    def test_rejects_unsampled_video(self):
        self.plan['blocks'][0]['shots'][0].update({'in': .2, 'duration': 1})
        with self.assertRaisesRegex(ValueError, '已观察的抽帧'):
            self.check()

    def test_rejects_duplicate_cue(self):
        block = self.plan['blocks'][1]
        block['narration'] = '然后训练模型，然后训练模型。'
        shot = copy.deepcopy(block['shots'][0])
        shot['cue'] = '然后'
        block['shots'].append(shot)
        with self.assertRaisesRegex(ValueError, '切点短语'):
            self.check()

    def test_rejects_paths_in_figure_ids(self):
        self.plan['figures'][0]['id'] = '../../outside'
        with self.assertRaisesRegex(ValueError, '格式无效'):
            self.check()

    def test_requires_both_media_types(self):
        self.plan['blocks'][0]['shots'][0] = copy.deepcopy(self.plan['blocks'][1]['shots'][0])
        with self.assertRaisesRegex(ValueError, '同时包含'):
            self.check()

    def test_rejects_extreme_narration_length(self):
        with self.assertRaisesRegex(ValueError, '口播总长度'):
            pv.validate_plan(self.plan, self.pages, self.info, seconds=180)

    def test_arxiv_normalization(self):
        self.assertEqual(pv.normalize_paper_url('https://arxiv.org/abs/1706.03762v7'), 'https://arxiv.org/pdf/1706.03762v7')

    def test_extracts_pdf_meta_and_video_source(self):
        parser = pv.MediaLinks()
        parser.feed('<meta name="citation_pdf_url" content="paper.pdf"><video><source src="demo.mp4"></video>')
        self.assertEqual(parser.pdf, ['paper.pdf'])
        self.assertEqual(parser.video, ['demo.mp4'])

    def test_pdf_crop_and_project_export(self):
        import pymupdf
        from PIL import Image
        with tempfile.TemporaryDirectory(prefix='paper_video_') as folder:
            job = Path(folder)
            (job / 'assets').mkdir()
            pdf = job / 'assets/paper.pdf'
            with pymupdf.open() as document:
                page = document.new_page(width=600, height=800)
                page.insert_text((50, 50), self.pages[0]['text'])
                page.draw_rect(pymupdf.Rect(70, 200, 520, 600))
                document.save(pdf)
            settings = {'paper': str(pdf), 'video': 'demo.mp4', 'voice': 'zh-CN-YunxiNeural', 'rate': '-10%'}
            config = pv.export_project(self.plan, pdf, job, settings)
            self.assertEqual(config['output_name'], 'video.mp4')
            self.assertEqual(config['blocks'][0]['shots'][0]['file'], 'assets/source_video.mp4')
            self.assertNotIn('highlight', config['blocks'][1]['shots'][0])
            with Image.open(job / 'assets/figure_fig1.png') as image:
                self.assertEqual(image.size, (960, 960))
            self.assertTrue((job / 'narration.txt').is_file())
            self.assertTrue((job / 'evidence.json').is_file())

    def test_analysis_returns_individual_pdf_pages(self):
        import pymupdf
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            pdf = root / 'paper.pdf'
            with pymupdf.open() as document:
                page = document.new_page()
                page.insert_textbox(pymupdf.Rect(30, 30, 500, 700), ('This is extracted source text for the paper. ' * 10))
                document.save(pdf)
            pages, images = pv.inspect_pdf(pdf, root)
            self.assertEqual(len(pages), 1)
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].name, 'page_001.png')

    def test_crop_expands_to_complete_vector_container(self):
        import pymupdf
        with pymupdf.open() as document:
            page = document.new_page(width=600, height=800)
            page.draw_rect(pymupdf.Rect(65, 330, 530, 530))
            rect = pv.expand_clipped_objects(page, pymupdf.Rect(30, 316, 270, 552))
            self.assertGreaterEqual(rect.x1, 530)
            self.assertLessEqual(rect.x0, 65)

    def test_renderer_rejects_output_path_before_any_rendering(self):
        from make_video import build
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'project.json'
            path.write_text(json.dumps({'output_name': '../outside.mp4'}), encoding='utf-8')
            with self.assertRaisesRegex(RuntimeError, 'output_name'):
                asyncio.run(build(SimpleNamespace(project=str(path))))
            self.assertFalse((Path(folder) / 'output').exists())


class SourceDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/page':
                    content, kind = b'<meta name="citation_pdf_url" content="/paper.pdf">', 'text/html'
                elif self.path == '/ambiguous':
                    content, kind = b'<video src="a.mp4"></video><video src="b.mp4"></video>', 'text/html'
                else:
                    content, kind = b'%PDF-test-download', 'application/pdf'
                self.send_response(200)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *_):
                pass

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_download_follows_relative_pdf_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'paper.pdf'
            resolved = pv.acquire(self.url + '/page', target, 'pdf')
            self.assertEqual(resolved, self.url + '/paper.pdf')
            self.assertEqual(target.read_bytes(), b'%PDF-test-download')
            self.assertFalse(target.with_suffix('.pdf.part').exists())

    def test_rejects_ambiguous_video_page(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, '唯一'):
                pv.acquire(self.url + '/ambiguous', Path(folder) / 'video.mp4', 'video')

    def test_copy_local_unicode_path(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / '中文论文.pdf', Path(folder) / 'copy.pdf'
            source.write_bytes(b'%PDF-local')
            pv.acquire('"' + str(source) + '"', target, 'pdf')
            self.assertEqual(source.read_bytes(), target.read_bytes())


if __name__ == '__main__':
    unittest.main()
