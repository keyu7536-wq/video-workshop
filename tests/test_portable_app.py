import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import app_config
from studio_render import topic_label
from studio_server import validate_settings

class PortableAppTests(unittest.TestCase):
    def test_public_config_is_optional_and_portable(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'app.local.json'
            with patch.dict(os.environ,{'VIDEO_WORKSHOP_CONFIG':str(path)}):
                self.assertEqual(app_config.load_config()['name'],'视频工坊')
                path.write_text(json.dumps({'name':'新工作台','port':9000,'default_rate':'+10%'}),encoding='utf-8')
                self.assertEqual(app_config.load_config()['port'],9000)
                path.write_text(json.dumps({'api_key':'private'}),encoding='utf-8')
                with self.assertRaises(ValueError):app_config.load_config()

    def test_editorial_labels_leave_concrete_topics_and_narration_intact(self):
        for prefix in ('先讲清：','核心想法：','先看结论：','关键结论：','最后总结：'):
            self.assertEqual(topic_label(prefix+'身体状态预测'),'身体状态预测')
        self.assertEqual(topic_label('这篇论文真正推进了什么'),'')
        self.assertEqual(topic_label('实验：样本内准确率'),'实验：样本内准确率')
        from studio_render import write_subtitles
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            write_subtitles(root,root,'Arial','标题',[{'start':0,'end':2,'chapter':'核心想法：状态预测'}],[(0,2,'先讲清：这是用户给定的原文。')],2,1920,1080,{'layout_style':'immersive'})
            self.assertIn('先讲清：这是用户给定的原文。',(root/'subtitles.srt').read_text(encoding='utf-8'))
            self.assertNotIn('核心想法：状态预测',(root/'captions.ass').read_text(encoding='utf-8'))

    def test_pdf_is_not_forced_to_be_a_paper(self):
        import studio_engine as engine
        prompts=[]
        def review(prompt,*args):
            prompts.append(prompt);return {'ready':True,'summary':'ok','issues':[]}
        with tempfile.TemporaryDirectory() as folder,patch.object(engine,'call_ai',side_effect=review):
            job=Path(folder)
            engine.review_plan(job,{'blocks':[]},[{'type':'page'}],{'content_type':'general'})
            self.assertNotIn('缺少研究任务或核心机制',prompts[-1])
            engine.review_plan(job,{'blocks':[]},[{'type':'page'}],{'content_type':'auto'})
            self.assertIn('PDF格式本身不代表论文',prompts[-1])
        for mode in ('auto','paper','general'):validate_settings({'content_type':mode})
        with self.assertRaises(ValueError):validate_settings({'content_type':'invented'})
