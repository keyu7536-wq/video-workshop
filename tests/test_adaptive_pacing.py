import argparse
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import studio_voice as voice


class PhrasingTests(unittest.TestCase):
    def test_default_is_a_baseline_with_small_bounded_changes(self):
        text = '这一段先展示完整动作，再把注意力放在发生变化的位置。\n成功率为91.5%，比较包含三个条件。\n为什么会出现差别？'
        parts = voice.delivery_segments(text)
        rates = [int(part['rate'].rstrip('%')) for part in parts]
        self.assertEqual(''.join(part['text'] for part in parts), text)
        self.assertGreaterEqual(min(rates), 34)
        self.assertLessEqual(max(rates), 40)
        self.assertLess(rates[1], rates[0])
        self.assertTrue(all(abs(a-b) <= 6 for a, b in zip(rates, rates[1:])))

    def test_explicit_baseline_applies_without_modifying_text(self):
        text = '先看完整动作。\n共有12个测试条件。'
        normal = voice.delivery_segments(text, base_rate='+40%')
        faster = voice.delivery_segments(text, base_rate='+50%')
        self.assertEqual([p['text'] for p in normal], [p['text'] for p in faster])
        self.assertTrue(all(int(b['rate'][:-1])-int(a['rate'][:-1]) == 10
                            for a, b in zip(normal, faster)))

    def test_short_sentences_and_commas_share_a_phrase_instead_of_separate_voices(self):
        text = '先看全图。再看左边。两边颜色不同，但物体相同，移动方向也没有变。'
        parts = voice.delivery_segments(text)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]['text'], text)
        self.assertEqual(parts[0]['pause_ms'], 0)

    def test_decimal_abbreviation_version_and_grouped_number_are_never_split(self):
        text = ('  Dr. Chen uses U.S. data, e.g. version v2.1 with 1,024.5 samples. '
                'Next, compare the reference output.\r\n\r\n'
                '数值为0.05，不是0.5；MBRL 的名字保持不变。  ')
        parts = voice.delivery_segments(text)
        self.assertEqual(''.join(part['text'] for part in parts), text)
        for token in ('Dr. Chen', 'U.S.', 'e.g.', 'v2.1', '1,024.5', '0.05', '0.5', 'MBRL'):
            self.assertTrue(any(token in part['text'] for part in parts), token)
        self.assertFalse(any(part['text'].rstrip().endswith(('Dr.', 'e.', 'U.', '1,')) for part in parts))

    def test_long_comma_sentence_is_split_only_after_substantial_clauses(self):
        clause = '画面保持同一个观察方向，旁白说明这一部分发生的变化，同时保留需要比较的对象'
        text = '，'.join([clause] * 7) + '。'
        parts = voice.delivery_segments(text)
        self.assertEqual(''.join(part['text'] for part in parts), text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p['text'].strip()) >= 28 for p in parts[:-1]))
        self.assertLess(len(parts), text.count('，'))

    def test_no_punctuation_does_not_split_a_word_or_invent_text(self):
        text = 'Supercalifragilisticexpialidocious' * 10
        parts = voice.delivery_segments(text)
        self.assertEqual([p['text'] for p in parts], [text])

    def test_url_query_stays_in_one_phrase_when_other_sentences_are_long(self):
        url = 'https://example.org/v2.1/data?a=1,024.5&b=2'
        text = 'This is a sufficiently long introductory sentence about the material. ' + url + ' provides the original source. Continue with a separate explanation.'
        parts = voice.delivery_segments(text)
        self.assertEqual(''.join(p['text'] for p in parts), text)
        self.assertTrue(any(url in p['text'] for p in parts))

    def test_leading_trailing_and_blank_line_whitespace_is_preserved(self):
        for text in ('\n  第一段。\n\n第二段。  \n', '\t标题\r\n\r\n正文，没有删字。\t'):
            parts = voice.delivery_segments(text)
            self.assertEqual(''.join(p['text'] for p in parts), text)
            self.assertEqual(parts[-1]['pause_ms'], 0)
            self.assertTrue(all(0 <= p['pause_ms'] <= 160 for p in parts))
        self.assertEqual(voice.delivery_segments(' \r\n\t'), [])

    def test_invalid_baselines_fail_before_synthesis(self):
        for rate in ('auto', 'fast', '+81%', '-31%', 40, True, [], '+4.5%'):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                voice.delivery_segments('原稿。', base_rate=rate)
        self.assertEqual(voice.normalize_base_rate(None), '+40%')
        self.assertEqual(voice.normalize_base_rate('40%'), '+40%')
        self.assertEqual(voice.normalize_base_rate('-0%'), '+0%')


class AdaptivePCMTests(unittest.TestCase):
    def test_sample_offsets_pauses_and_original_boundary_durations_are_exact(self):
        from make_video import RATE_HZ
        text = '第一段保留。\n数值为12.5。\n第三段继续。'
        source_events = [[{'type': 'WordBoundary', 'offset': 31, 'duration': 97, 'text': '词'},
                          {'type': 'marker', 'text': '未带时间'}] for _ in range(3)]
        events_before = copy.deepcopy(source_events)
        audio = [b'\x01\x00' * 13, b'\x02\x00' * 29, b'\x03\x00' * 41]
        synth = AsyncMock(side_effect=[(audio[0], source_events[0], True),
                                       (audio[1], source_events[1], False),
                                       (audio[2], source_events[2], True)])
        with patch('make_video.make_voice', synth):
            pcm, events, cached, timing = asyncio.run(
                voice.make_adaptive_voice(text, 'unchanged-voice', Path('.'), 'ffmpeg', '+50%'))
        self.assertFalse(cached)
        self.assertEqual(len(timing), 3)
        samples = 0
        for index, item in enumerate(timing):
            self.assertEqual(item['start_sample'], samples)
            self.assertEqual(item['start'], samples / RATE_HZ)
            self.assertEqual(item['audio_samples'], len(audio[index]) // 2)
            self.assertEqual(events[index * 2]['offset'], 31 + (samples * 10_000_000 + RATE_HZ // 2) // RATE_HZ)
            self.assertEqual(events[index * 2]['duration'], 97)
            self.assertEqual(pcm[samples * 2:samples * 2 + len(audio[index])], audio[index])
            self.assertEqual(item['provider'], 'edge-tts')
            self.assertEqual(item['voice'], 'unchanged-voice')
            self.assertEqual(item['base_rate'], '+50%')
            self.assertEqual(item['pacing_version'], voice.ADAPTIVE_PACING_VERSION)
            pause_start = samples * 2 + len(audio[index])
            self.assertEqual(pcm[pause_start:pause_start + item['pause_samples'] * 2],
                             b'\x00' * item['pause_samples'] * 2)
            samples += item['audio_samples'] + item['pause_samples']
        self.assertEqual(len(pcm), samples * 2)
        self.assertEqual(timing[-1]['pause_samples'], 0)
        self.assertEqual(source_events, events_before)
        self.assertEqual(''.join(call.args[0] for call in synth.await_args_list), text)
        self.assertTrue(all(call.args[1] == 'unchanged-voice' for call in synth.await_args_list))

    def test_cached_audio_retains_the_same_plan_and_boundaries(self):
        async def cached(*args):
            return b'\x01\x00' * 240, [{'offset': 0, 'duration': 100000}], True
        with patch('make_video.make_voice', side_effect=cached) as synth:
            first = asyncio.run(voice.make_adaptive_voice('保留文案。\n第二段。', 'same', Path('.'), 'ffmpeg'))
            second = asyncio.run(voice.make_adaptive_voice('保留文案。\n第二段。', 'same', Path('.'), 'ffmpeg'))
        self.assertEqual(first, second)
        self.assertTrue(first[2])
        self.assertEqual(synth.await_count, 4)

    def test_failure_does_not_change_voice_rate_or_retry_with_a_different_narrator(self):
        synth = AsyncMock(side_effect=[(b'\x01\x00' * 240, [], False), RuntimeError('provider unavailable')])
        with patch('make_video.make_voice', synth), self.assertRaisesRegex(RuntimeError, 'provider unavailable'):
            asyncio.run(voice.make_adaptive_voice('先说这一段。\n第二段继续。\n第三段。', 'one-voice', Path('.'), 'ffmpeg'))
        self.assertEqual(synth.await_count, 2)
        self.assertTrue(all(call.args[1] == 'one-voice' for call in synth.await_args_list))

    def test_invalid_pcm_or_empty_script_cannot_create_a_fake_success(self):
        for pcm in (b'', b'\x00'):
            with self.subTest(pcm=pcm), patch('make_video.make_voice', new=AsyncMock(return_value=(pcm, [], False))), self.assertRaises(ValueError):
                asyncio.run(voice.make_adaptive_voice('有声文本。', 'voice', Path('.'), 'ffmpeg'))
        with patch('make_video.make_voice', new=AsyncMock()) as synth, self.assertRaises(ValueError):
            asyncio.run(voice.make_adaptive_voice(' \n ', 'voice', Path('.'), 'ffmpeg'))
        synth.assert_not_awaited()

    def test_fixed_rate_build_still_calls_the_original_provider_with_exact_rate(self):
        import make_video
        class ReachedFixedProvider(Exception):
            pass
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'image.png').write_bytes(b'fixture source exists; rendering is not reached')
            project = {'voice': 'zh-CN-YunyangNeural', 'rate': '+50%', 'fps': 30,
                       'adaptive_base_rate': '+25%', 'title': 'fixture',
                       'blocks': [{'narration': '固定语速保留原稿。', 'shots': [{'kind': 'image', 'file': 'image.png'}]}]}
            path = root / 'project.json'; path.write_text(json.dumps(project), encoding='utf-8')
            args = argparse.Namespace(project=str(path), check=False, font=None, voice=None, rate=None)
            fixed = AsyncMock(side_effect=ReachedFixedProvider)
            with patch.object(make_video, 'ffmpeg_path', return_value='ffmpeg'), \
                 patch.object(make_video, 'find_font', return_value='fixture-font'), \
                 patch.object(make_video, 'log'), patch.object(make_video, 'make_voice', fixed), \
                 patch.object(voice, 'make_adaptive_voice', new=AsyncMock()) as adaptive:
                with self.assertRaises(ReachedFixedProvider):
                    asyncio.run(make_video.build(args))
            fixed.assert_awaited_once()
            self.assertEqual(fixed.await_args.args[:3], ('固定语速保留原稿。', 'zh-CN-YunyangNeural', '+50%'))
            adaptive.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
