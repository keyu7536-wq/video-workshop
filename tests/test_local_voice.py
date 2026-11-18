import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import local_voice_worker as worker

class SpeechChecks(unittest.TestCase):
    def test_optional_demonstrative_classifier_is_not_experimental_number(self):
        expected='三小时失败二十五段，九点八小时后为零，覆盖这批动作。'
        actual='三小时失败二十五段九点八小时后为零覆盖这一批动作'
        self.assertTrue(worker.compare_speech(expected,actual)['passed'])
        self.assertFalse(worker.compare_speech(expected,actual.replace('二十五','二十六'))['passed'])
    def setUp(self):
        try:import cn2an,opencc
        except ImportError:self.skipTest('Run this file with .venv-voice for real speech normalization tests')
    def test_percent_half_second_and_traditional_text(self):
        result=worker.compare_speech('成功率百分之九十一，半秒更新。','成功率91%，0.5秒更新。')
        self.assertTrue(result['passed'])
        self.assertTrue(worker.compare_speech('机器人怎么找到椅子？','機器人怎麼找到椅子？')['passed'])
    def test_wrong_results_fail_even_if_rest_matches(self):
        result=worker.compare_speech('在六个环境里做了三百六十次测试，平均成功率百分之九十一。','在六个环境里做了三百六十次测试，平均成功率百分之十九。')
        self.assertFalse(result['passed']);self.assertFalse(result['numbers_equal'])
    def test_missing_sentence_fails(self):
        self.assertFalse(worker.compare_speech('机器人学会走路，还要在陌生房间找到椅子并坐下。','机器人学会走路。')['passed'])
    def test_missing_punctuation_does_not_merge_adjacent_quantities(self):
        expected='成功率百分之八十五；六种单任务合计三百六十次。'
        self.assertTrue(worker.compare_speech(expected,'成功率百分之八十五六种单任务合计三百六十次')['passed'])
        self.assertFalse(worker.compare_speech(expected,'成功率百分之八十五六种单任务合计三百六十四')['passed'])

if __name__=='__main__':unittest.main()
