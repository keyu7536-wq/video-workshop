import copy
import tempfile
import unittest
from pathlib import Path

from studio_storyboard import validate_story,check_understanding


def plan():
    return {'story':{'question':'为什么要先预测？','answer':'比较选择再行动。',
        'example':{'description':'小车选路','source_ids':[],'illustrative':True},
        'steps':[{'block_id':'a','depends_on':'','change':'先比较路径','why':''},
                 {'block_id':'b','depends_on':'a','change':'再按结果行动','why':'比较后能选出目标路径'}]},
        'blocks':[{'id':'a','narration':'假设一辆小车来到岔路口。','shots':[]},
                  {'id':'b','narration':'先比较两条路线，再作选择。','shots':[]}]}


class StoryboardTests(unittest.TestCase):
    def test_legacy_and_source_integrity(self):
        p=plan();before=copy.deepcopy(p)
        validate_story(p,[]);self.assertEqual(p,before)
        validate_story({'blocks':[]},[])
        p['story']['example']['illustrative']=False
        with self.assertRaisesRegex(ValueError,'关联资料'):validate_story(p,[])

    def test_dependency_and_unknown_source(self):
        p=plan();p['story']['steps'][0]['depends_on']='b'
        with self.assertRaisesRegex(ValueError,'前面的段落'):validate_story(p,[])
        p=plan();p['story']['example']['source_ids']=['invented']
        with self.assertRaisesRegex(ValueError,'不存在'):validate_story(p,[])

    def test_reader_does_not_get_authored_answers(self):
        p=plan();p['story']['answer']='SECRET_EXPECTED_ANSWER'
        p['blocks'][0]['explanation']={'takeaway':'SECRET_EXPLANATION'}
        def ai(prompt,images,analysis,request,schema,suffix):
            self.assertNotIn('SECRET_',prompt)
            self.assertIn('假设一辆小车',prompt)
            return {'question':'如何选择？','answer_from_video':'比较后选择。',
                    'connections':[{'block_id':bid,'understood':True,'explanation_from_video':'比较路线','missing':''} for bid in ('a','b')],
                    'issues':[]}
        with tempfile.TemporaryDirectory() as tmp:
            result=check_understanding(Path(tmp),p,{},ai)
        self.assertEqual(result['status'],'completed')

    def test_unavailable_is_not_a_pass(self):
        def fail(*args,**kwargs):raise RuntimeError('offline')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError,'offline'):check_understanding(Path(tmp),plan(),{},fail)
            self.assertIsNone(check_understanding(Path(tmp),{'blocks':[]},{},fail))

    def test_reject_review_hallucinated_block(self):
        def ai(*args):
            return {'question':'q','answer_from_video':'a','connections':[],
                    'issues':[{'severity':'error','block_id':'invented','reason':'missing'}]}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'不存在'):check_understanding(Path(tmp),plan(),{},ai)


if __name__=='__main__':unittest.main()
