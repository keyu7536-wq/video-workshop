"""Real-browser checks for independently mounted story and media editors."""
import contextlib
import importlib.util
from pathlib import Path
import unittest


@unittest.skipUnless(importlib.util.find_spec('playwright'),'Playwright is not installed')
class StorylineUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.resources=contextlib.ExitStack()
        cls.addClassCleanup(cls.resources.close)
        cls.playwright=sync_playwright().start()
        cls.resources.callback(cls.playwright.stop)
        executable=Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
        try:
            cls.browser=cls.playwright.chromium.launch(headless=True,
                **({'executable_path':str(executable)} if executable.is_file() else {}))
        except Exception as exc:
            raise unittest.SkipTest(f'Headless browser unavailable: {exc}')
        cls.resources.callback(cls.browser.close)

    def setUp(self):
        self.page=self.browser.new_page(viewport={'width':1000,'height':850})
        self.addCleanup(self.page.close)
        self.page.set_default_timeout(4000)
        self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.page.set_content('''<main><div id="storyboard"></div></main>
          <form id="shotForm"><select id="shotSource"><option value="v1">视频一</option>
            <option value="v2">视频二</option><option value="i1">图片</option></select>
            <div class="production-options"><input id="shotIn" type="number" value="0" step="0.1">
            <input id="shotDuration" type="number" value="4" step="0.1"></div></form>''')
        self.page.add_script_tag(content='''
          const $=id=>document.getElementById(id);
          const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
          const state={dirty:false,job:{id:'fixture',sources:[{id:'v1',type:'video',name:'第一段原片'},
            {id:'v2',type:'video',name:'第二段原片'},{id:'i1',type:'image',name:'原图'}],
            plan:{blocks:[{id:'b1',chapter:'观察对象',narration:'这是保持不变的第一段。'},
                          {id:'b2',chapter:'比较变化',narration:'这是保持不变的第二段。'}]},
            media_index:{status:'partial',segments:[{id:'seg1',source_id:'v1',start:1.25,end:8.75,
              description:'左侧机器人在不同采样帧中姿态不同。',visual_role:'demonstration',
              observed_times:[2,4,6,8],limitations:['四栏画面；只看了采样帧。']},
              {id:'long',source_id:'v1',start:0,end:60,description:'较长候选，观察点在后半段。',
              visual_role:'process',observed_times:[40,50],limitations:['未核验连续运动。']}],
              gaps:[{source_id:'v1',reason:'部分区间没有观察。'}]}}};
          let previewUpdates=0;
          function renderPlanReview(){} function updateControls(){} function renderMeasuredSegments(){}
          function updateShotPreview(){previewUpdates++;}
        ''')
        self.page.add_script_tag(path=str(Path(__file__).resolve().parents[1]/'studio/storyline.js'))
        self.page.evaluate('initializeStorylineEditor();renderStoryline();')

    def tearDown(self):
        self.assertEqual(self.errors,[])

    def open_story(self):
        self.page.locator('#storylinePanel > summary').click()
        return self.page.locator('#storylinePanel')

    def open_candidates(self):
        self.page.evaluate('renderMediaSelection({});')
        self.page.locator('#mediaSelectionPanel details').first.locator('summary').click()

    def select_first(self):
        self.open_candidates()
        self.page.locator('[data-media-candidate="0"]').click()

    def read_selection(self):
        return self.page.evaluate("readMediaSelection($('shotSource').value,Number($('shotIn').value),Number($('shotDuration').value))")

    def test_opening_legacy_story_does_not_modify_plan_or_narration(self):
        before=self.page.evaluate('JSON.stringify(state.job.plan)')
        self.open_story()
        self.assertFalse(self.page.locator('[data-story-enabled]').is_checked())
        self.assertFalse(self.page.evaluate('state.dirty'))
        self.assertEqual(self.page.evaluate('JSON.stringify(state.job.plan)'),before)
        self.assertFalse(self.page.evaluate("Object.hasOwn(state.job.plan,'story')"))

    def test_main_question_example_and_connections_are_editable_without_rewriting(self):
        before=self.page.evaluate('state.job.plan.blocks.map(block=>block.narration)')
        panel=self.open_story()
        panel.locator('[data-story-enabled]').check()
        panel.locator('[data-story-field="question"]').fill('这个变化如何发生？')
        panel.locator('[data-story-field="answer"]').fill('从同一对象的前后状态说明变化。')
        panel.locator('[data-story-field="example"]').fill('用一辆示意小车贯穿两段。')
        panel.locator('[data-story-field="illustrative"]').check()
        panel.locator('[data-story-field="source_ids"]').select_option(['i1'])
        for index in range(2):
            row=panel.locator(f'[data-story-step="{index}"]')
            row.locator('summary').click()
            row.locator('[data-step-field="change"]').fill(f'新理解{index+1}')
            row.locator('[data-step-field="why"]').fill(f'衔接依据{index+1}')
        panel.locator('[data-story-step="1"] [data-step-field="depends_on"]').select_option('b1')
        story=self.page.evaluate('state.job.plan.story')
        self.assertEqual(story['question'],'这个变化如何发生？')
        self.assertTrue(story['example']['illustrative'])
        self.assertEqual(story['example']['source_ids'],['i1'])
        self.assertEqual(story['steps'][1],{'block_id':'b2','depends_on':'b1','change':'新理解2','why':'衔接依据2'})
        self.assertEqual(self.page.evaluate('state.job.plan.blocks.map(block=>block.narration)'),before)
        self.assertTrue(self.page.evaluate('state.dirty'))
        self.assertEqual(panel.locator('[data-story-step="0"] [data-step-field="depends_on"] option').count(),1)

    def test_disabled_story_is_null_and_rendering_is_idempotent(self):
        panel=self.open_story();panel.locator('[data-story-enabled]').check()
        panel.locator('[data-story-field="question"]').fill('问题')
        panel.locator('[data-story-enabled]').uncheck()
        self.assertIsNone(self.page.evaluate('state.job.plan.story'))
        self.page.evaluate('renderStoryline();renderStoryline();')
        self.assertEqual(self.page.locator('#storylinePanel').count(),1)

    def test_candidate_limits_and_observed_scope_are_shown_and_reason_required(self):
        self.select_first()
        self.assertIn('四栏画面',self.page.locator('#mediaSelectionPanel').inner_text())
        self.assertIn('部分区间没有观察',self.page.locator('#mediaSelectionPanel').inner_text())
        self.assertEqual(float(self.page.locator('#shotIn').input_value()),1.25)
        self.assertEqual(float(self.page.locator('#shotDuration').input_value()),7.5)
        error=self.page.evaluate("(()=>{try{readMediaSelection('v1',1.25,7.5);return '';}catch(e){return e.message;}})()")
        self.assertIn('哪句话',error)
        self.page.locator('#mediaSelectionReason').fill('对应这一句中的机器人姿态变化。')
        self.assertEqual(self.read_selection(),{'media_segment_id':'seg1','selection_reason':'对应这一句中的机器人姿态变化。'})
        self.assertFalse(self.page.evaluate('state.dirty'),'Dialog drafting must wait for the normal save action')
        self.assertTrue(self.page.locator('#shotIn').evaluate('(element)=>element.checkValidity()'))

    def test_manual_timing_change_clears_binding_even_if_inside_original_candidate(self):
        self.select_first();self.page.locator('#mediaSelectionReason').fill('对应姿态变化。')
        self.page.locator('#shotIn').fill('2')
        self.assertEqual(self.read_selection(),{'media_segment_id':'','selection_reason':''})
        self.assertIn('原片段关联已取消',self.page.locator('#mediaSelectionHint').inner_text())

    def test_source_switch_clears_selection_and_unavailable_index_keeps_manual_option(self):
        self.select_first();self.page.locator('#mediaSelectionReason').fill('对应姿态变化。')
        self.page.locator('#shotSource').select_option('v2')
        self.assertIn('手动设置',self.page.locator('#mediaSelectionPanel').inner_text())
        self.assertEqual(self.read_selection(),{'media_segment_id':'','selection_reason':''})
        self.page.locator('#shotSource').select_option('i1')
        self.assertTrue(self.page.locator('#mediaSelectionPanel').is_hidden())

    def test_long_candidate_is_limited_to_renderable_window_with_observed_time(self):
        self.page.evaluate('renderMediaSelection({});')
        self.page.locator('#mediaSelectionPanel details').nth(1).locator('summary').click()
        self.page.locator('[data-media-candidate="1"]').click()
        start=float(self.page.locator('#shotIn').input_value())
        duration=float(self.page.locator('#shotDuration').input_value())
        self.assertLessEqual(duration,20)
        self.assertTrue(any(start<=time<start+duration for time in [40,50]))
        self.page.locator('#mediaSelectionReason').fill('对应后半段可见状态。')
        self.assertEqual(self.read_selection()['media_segment_id'],'long')

    def test_reopening_restores_reason_and_programmatic_timing_mismatch_cannot_rebind(self):
        self.page.evaluate("$('shotIn').value=2;$('shotDuration').value=4;renderMediaSelection({media_segment_id:'seg1',selection_reason:'已有理由'});")
        self.assertEqual(self.page.locator('#mediaSelectionReason').input_value(),'已有理由')
        self.assertEqual(self.read_selection()['media_segment_id'],'seg1')
        error=self.page.evaluate("(()=>{try{readMediaSelection('v1',3,4);return '';}catch(e){return e.message;}})()")
        self.assertIn('重新选择',error)

    def test_source_and_description_markup_are_text_not_executable_html(self):
        self.page.evaluate("state.job.media_index.segments[0].description='<img src=x onerror=window.injected=true>';renderMediaSelection({});")
        self.assertEqual(self.page.locator('#mediaSelectionPanel img').count(),0)
        self.assertFalse(self.page.evaluate('Boolean(window.injected)'))


if __name__=='__main__':
    unittest.main()
