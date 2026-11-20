"""Real-browser regression checks for the independent motion editor."""
import contextlib
import importlib.util
from pathlib import Path
import unittest

from studio_motion import validate_motion
from tests.test_motion import parallel


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is not installed')
class MotionEditorUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.resources = contextlib.ExitStack(); cls.addClassCleanup(cls.resources.close)
        runtime = sync_playwright().start(); cls.resources.callback(runtime.stop)
        executable = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
        cls.browser = runtime.chromium.launch(headless=True,
            **({'executable_path': str(executable)} if executable.is_file() else {}))
        cls.resources.callback(cls.browser.close)
        cls.script = (Path(__file__).resolve().parents[1]/'studio/motion.js').read_text(encoding='utf-8')

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width': 900, 'height': 1000})
        self.addCleanup(self.page.close)
        self.errors = []; self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.set_content('''<!doctype html><meta charset="utf-8"><style>
          body{background:#111925;color:#eee;font:16px sans-serif;margin:16px}
          input,select,button{box-sizing:border-box;font:inherit;padding:8px}
          label{display:block}button{margin:4px}form{max-width:800px}</style>
          <form id="shotForm"><label>画面来源<select id="shotSource">
          <option value="motion">连续图解</option><option value="card">文字卡片</option>
          </select></label></form>''')
        self.page.evaluate('''() => {
          window.state={shot:[0,0],job:{plan:{blocks:[{narration:'先看两项工作，各自推进，再继续处理。'}]}}};
          window.displayDimensions=()=>[960,540];
        }''')
        self.page.add_script_tag(content=self.script)
        self.original = {'kind': 'motion', 'motion': parallel()}
        self.page.evaluate('shot=>{window.originalShot=shot;initializeMotionEditor();openMotionEditor(shot);}', self.original)

    def read(self):
        return self.page.evaluate('readMotionEditor()')

    def failure(self):
        return self.page.evaluate('''() => {try {readMotionEditor();return null;}catch(e){return e.message;}}''')

    def test_editing_copy_and_preview_does_not_mutate_original(self):
        self.page.get_by_label('对象1名称', exact=True).fill('新的对象名')
        self.page.get_by_label('对象1形状', exact=True).select_option('rect')
        self.page.get_by_label('对象1颜色', exact=True).select_option('purple')
        self.page.get_by_label('图解标题', exact=True).fill('连续的位置变化')
        result = self.read()
        self.assertEqual(result['objects'][0], {'id': 'a', 'shape': 'rect', 'label': '新的对象名', 'color': 'purple'})
        self.assertEqual(self.page.evaluate('originalShot'), self.original)
        self.assertIn('连续的位置变化', self.page.locator('#motionPreview').text_content())
        self.assertIn('静态布局预览', self.page.locator('#motionEditor').inner_text())
        self.assertEqual(self.errors, [])

    def test_stage_positions_and_cues_remain_editable_without_runtime_times(self):
        self.page.locator('#motionStageSelect').select_option('1')
        self.page.get_by_label('整理素材横向位置', exact=True).fill('48.5')
        self.page.get_by_label('整理素材纵向位置', exact=True).fill('34')
        self.page.get_by_label('整理素材本阶段名称', exact=True).fill('处理中')
        result = self.read(); pose = result['stages'][1]['poses'][0]
        self.assertEqual(pose['x'], .485); self.assertEqual(pose['y'], .34)
        self.assertEqual(pose['label'], '处理中')
        self.assertFalse(any('start' in stage for stage in result['stages']))
        validate_motion(result, '先看两项工作，各自推进，再继续处理。', 960, 540)
        self.assertEqual(self.errors, [])

    def test_invalid_numeric_inputs_and_missing_cues_are_not_silently_replaced(self):
        self.page.get_by_label('整理素材横向位置', exact=True).fill('')
        self.assertIn('0～100', self.failure())
        self.page.get_by_label('整理素材横向位置', exact=True).fill('20')
        self.page.locator('#motionStageSelect').select_option('1')
        self.page.locator('#motionStageCue').fill('未出现的文字')
        self.assertIn('只出现一次', self.failure())
        self.page.locator('#motionStageCue').fill('继续处理')
        self.assertIn('不同的旁白', self.failure())
        self.assertEqual(self.page.evaluate('originalShot'), self.original)

    def test_add_remove_stages_and_objects_keep_pose_and_link_references_consistent(self):
        self.page.locator('#motionAddObject').click()
        result = self.read(); identifier = result['objects'][-1]['id']
        self.assertTrue(all(not stage['poses'][-1]['visible'] for stage in result['stages']))
        self.page.locator('#motionAddStage').click()
        self.assertIn('后续阶段', self.failure())
        self.page.locator('#motionStageCue').fill('两项工作')
        result = self.read()
        self.assertEqual(len(result['stages']), 4)
        self.assertTrue(all(len(stage['poses']) == 3 for stage in result['stages']))
        self.page.locator('#motionDeleteStage').click()
        self.page.locator(f'[data-motion-object="{identifier}"]').get_by_role('button', name='删除对象').click()
        result = self.read()
        self.assertEqual(len(result['objects']), 2)
        self.assertTrue(all(len(stage['poses']) == 2 for stage in result['stages']))
        self.assertEqual(self.errors, [])

    def test_links_require_visible_distinct_endpoints(self):
        self.page.locator('#motionAddLink').click()
        result = self.read()
        self.assertEqual(result['stages'][0]['links'][0], {'source': 'a', 'target': 'b', 'label': ''})
        self.page.get_by_label('连接1终点', exact=True).select_option('a')
        self.assertIn('两个不同', self.failure())
        self.page.get_by_label('连接1终点', exact=True).select_option('b')
        self.page.get_by_label('核对来源显示', exact=True).uncheck()
        self.assertIn('已显示', self.failure())
        self.page.get_by_role('button', name='删除连接', exact=True).click()
        self.assertEqual(self.read()['stages'][0]['links'], [])

    def test_nonmotion_selection_returns_null_and_reopening_restores_saved_copy(self):
        self.page.get_by_label('对象1名称', exact=True).fill('未保存的修改')
        self.page.locator('#shotSource').select_option('card')
        self.assertIsNone(self.read())
        self.assertTrue(self.page.locator('#motionEditor').is_hidden())
        self.page.locator('#shotSource').select_option('motion')
        self.page.evaluate('openMotionEditor(originalShot)')
        self.assertEqual(self.read(), validate_motion(self.original['motion'],check_layout=False))
        self.page.evaluate('initializeMotionEditor()')
        self.assertEqual(self.page.locator('#motionEditor').count(), 1)

    def test_labels_are_text_and_mobile_editor_has_no_horizontal_overflow(self):
        self.page.get_by_label('对象1名称', exact=True).fill('<img onerror=1>')
        self.assertEqual(self.page.locator('#motionEditor img').count(), 0)
        self.assertIn('<img onerror=1>', self.page.locator('#motionPreview').text_content())
        self.page.set_viewport_size({'width': 390, 'height': 844})
        width = self.page.evaluate('({viewport:innerWidth,body:document.documentElement.scrollWidth})')
        self.assertLessEqual(width['body'], width['viewport'])
        self.assertEqual(self.errors, [])

    def test_concrete_shape_controls_match_svg_and_round_trip(self):
        self.page.get_by_label('对象1形状',exact=True).select_option('document')
        self.page.get_by_label('对象2形状',exact=True).select_option('photo')
        self.assertEqual(self.page.locator('#motionPreview [data-motion-shape="document"]').count(),1)
        self.assertEqual(self.page.locator('#motionPreview [data-motion-shape="document"] polygon').count(),2)
        self.assertEqual(self.page.locator('#motionPreview [data-motion-shape="photo"] circle').count(),1)
        self.assertEqual(self.page.locator('#motionPreview [data-motion-shape="photo"] polygon').count(),1)
        self.assertEqual([obj['shape'] for obj in self.read()['objects']],['document','photo'])
        self.page.evaluate("openMotionEditor({kind:'card'})")
        self.assertEqual(self.read()['objects'][0]['shape'],'document')
        self.assertEqual(self.errors,[])

    def test_explicit_stage_states_round_trip_and_have_matching_svg_marks(self):
        self.assertTrue(all(pose['state']=='neutral' for stage in self.read()['stages'] for pose in stage['poses']))
        self.assertEqual(self.page.locator('#motionPreview [data-motion-state]').count(),0)
        for value,tag in (('idle','line'),('active','path'),('done','polyline')):
            self.page.get_by_label('整理素材本阶段状态',exact=True).select_option(value)
            saved=self.read()
            self.assertEqual(saved['stages'][0]['poses'][0]['state'],value)
            self.assertEqual(saved['stages'][1]['poses'][0]['state'],'neutral')
            mark=self.page.locator(f'#motionPreview [data-motion-state="{value}"]')
            self.assertEqual(mark.count(),1)
            self.assertGreater(mark.locator(tag).count(),0)
        self.assertEqual(self.page.evaluate('originalShot'),self.original)
        self.assertEqual(self.errors,[])

    def test_enlarged_svg_geometry_preserves_label_area_and_bad_states_are_rejected(self):
        self.page.get_by_label('对象1形状',exact=True).select_option('photo')
        rect=self.page.locator('#motionPreview [data-motion-shape="photo"] rect')
        self.assertAlmostEqual(float(rect.get_attribute('width')),.195*540)
        self.assertAlmostEqual(float(rect.get_attribute('height')),.135*540)
        self.assertAlmostEqual(float(rect.get_attribute('y')),.33*540-.09*540)
        self.page.evaluate("() => {let shot=JSON.parse(JSON.stringify(originalShot));shot.motion.stages[0].poses[0].state='pretend';openMotionEditor(shot);}")
        self.assertIn('阶段状态',self.failure())
        self.assertEqual(self.errors,[])


if __name__ == '__main__':
    unittest.main()
