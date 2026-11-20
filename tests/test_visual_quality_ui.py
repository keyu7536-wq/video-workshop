"""Actual browser checks for the existing advisory panel and new statistics."""
import contextlib
import importlib.util
from pathlib import Path
import unittest

from studio_visual_quality import visual_quality_report
from tests.test_visual_quality_coverage import example, HASH_A


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is not installed')
class VisualQualityUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.resources=contextlib.ExitStack();cls.addClassCleanup(cls.resources.close)
        runtime=sync_playwright().start();cls.resources.callback(runtime.stop)
        executable=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
        cls.browser=runtime.chromium.launch(headless=True,
            **({'executable_path':str(executable)} if executable.is_file() else {}))
        cls.resources.callback(cls.browser.close)
        cls.script=(Path(__file__).resolve().parents[1]/'studio/director.js').read_text(encoding='utf-8')

    def setUp(self):
        self.page=self.browser.new_page();self.addCleanup(self.page.close)
        self.page.set_content('<main id="result"></main>')
        self.page.evaluate('''() => {
          window.state={dirty:false,job:{}};
          window.escapeHTML=text=>{let node=document.createElement('span');node.textContent=String(text);return node.innerHTML;};
        }''')
        self.page.add_script_tag(content=self.script)

    def show(self,report):
        self.page.evaluate('report=>{state.job.visual_quality=report;document.getElementById("result").innerHTML=visualQualityHTML();}',report)
        return self.page.locator('#result').text_content()

    def test_current_timeline_and_deduplicated_source_counts_are_distinct(self):
        plan,actual=example()
        sources=[{'id':'v','type':'video','file':'a.mp4','content_sha256':HASH_A},
                 {'id':'duplicate','type':'video','file':'b.mp4','content_sha256':HASH_A}]
        text=self.show(visual_quality_report(plan,sources,actual))
        self.assertIn('视频登记 2 个来源，按文件哈希为 1 份，已选 1 份',text)
        self.assertIn('素材视频 6.6 秒',text)
        self.assertIn('连续机制示意最长 64.8 秒',text)
        self.assertIn('不代表已观看内容或听音',text)

    def test_missing_hashes_and_planned_weights_are_not_rendered_as_certainty(self):
        plan,_=example()
        text=self.show(visual_quality_report(plan,[{'id':'v','type':'video','file':'a.mp4'}]))
        self.assertIn('独立总数未知',text)
        self.assertIn('1 个来源未去重',text)
        self.assertIn('实际时长由配音后确定',text)
        self.assertNotIn('素材视频 0.0 秒',text)
        self.assertIn('不是实测秒数',text)

    def test_legacy_empty_dirty_and_escaped_issue_text(self):
        self.assertEqual(self.show({'issues':[]}), '')
        self.show({'issues':[{'reason':'<img src=x onerror=alert(1)>原样文字'}]})
        self.assertEqual(self.page.locator('#result img').count(),0)
        self.assertIn('<img',self.page.locator('#result').text_content())
        self.page.evaluate('state.dirty=true')
        self.assertEqual(self.page.evaluate('visualQualityHTML()'),'')

    def test_metadata_panel_can_show_statistics_without_quality_warnings(self):
        text=self.show(visual_quality_report({'blocks':[{'id':'b','shots':[{'kind':'video','source_id':'v'}]}]},
            [{'id':'v','type':'video','file':'a.mp4','content_sha256':HASH_A}]))
        self.assertIn('素材与节奏',text)
        self.assertEqual(self.page.locator('.warning').count(),0)


if __name__=='__main__':unittest.main()
