"""Source-aware story continuity and an independent viewer comprehension check."""
from __future__ import annotations

import json
import paper_video as pv

STORYBOARD_VERSION = 1
S = {'type': 'string', 'maxLength': 500}
STORY_SCHEMA = {'anyOf': [{'type': 'null'}, pv.object_schema({
    'question': S, 'answer': S,
    'example': pv.object_schema({'description': S, 'source_ids': pv.array_schema({'type': 'string'}, 0, 12),
                                 'illustrative': {'type': 'boolean'}}),
    'steps': pv.array_schema(pv.object_schema({'block_id': {'type': 'string'},
        'depends_on': {'type': 'string'}, 'change': S, 'why': S}), 1, 24)
})]}
UNDERSTANDING_SCHEMA = pv.object_schema({
    'question': S, 'answer_from_video': S,
    'connections': pv.array_schema(pv.object_schema({'block_id': {'type': 'string'},
        'understood': {'type': 'boolean'}, 'explanation_from_video': S, 'missing': S}), 0, 24),
    'issues': pv.array_schema(pv.object_schema({'severity': {'type': 'string', 'enum': ['error', 'warning']},
                                              'block_id': {'type': 'string'}, 'reason': S}), 0, 24)
})


def validate_story(plan, sources):
    story = plan.get('story')
    if story is None:
        return
    from jsonschema import Draft202012Validator
    errors = list(Draft202012Validator(STORY_SCHEMA).iter_errors(story))
    if errors:
        raise ValueError('讲述主线格式错误：' + errors[0].message)
    if not story['question'].strip() or not story['answer'].strip():
        raise ValueError('讲述主线需要具体问题和回答。')
    known = {source['id'] for source in sources}
    example = story['example']
    if any(sid not in known for sid in example['source_ids']):
        raise ValueError('贯穿例子引用了不存在的来源。')
    if example['description'].strip() and not example['illustrative'] and not example['source_ids']:
        raise ValueError('来源中的例子需要关联资料；自行设想的例子须标为示意。')
    order = {block['id']: i for i, block in enumerate(plan['blocks'])}
    seen = set()
    for step in story['steps']:
        bid, dependency = step['block_id'], step['depends_on']
        if bid not in order or bid in seen:
            raise ValueError('讲述主线段落不存在或重复。')
        if dependency and (dependency not in order or order[dependency] >= order[bid]):
            raise ValueError('讲述衔接只能引用前面的段落。')
        if not step['change'].strip():
            raise ValueError('每个主线步骤需要说明观众新理解了什么。')
        seen.add(bid)


def story_instruction(request):
    return '''讲述主线 story 是整片的可编辑规划，不显示在成片上。自动写稿时先选一个具体中心问题 question，再写观众最终应得到的 answer；不是宽泛口号，也不强制问句开场。选一个能贯穿背景、方法和结果的具体对象或例子 example，前后保持对象、名词、颜色和条件一致。来自资料的例子关联 source_ids；自行设想的例子 illustrative=true，并在旁白自然说清“假设/比如”，不得伪装成实测动作或结果。没有适用例子时 description 为空，不为凑框架编造。
steps 按 block_id 记录每段带来的新理解 change、它依赖的前一段 depends_on（无则空）和为什么能推进到这里 why。机制类讲解先说明具体对象和问题，再顺着一次输入、变化、判断或反馈讲清它怎么起作用；不能只报“编码器、模型、策略”等名词。结果先交代比较对象、指标含义和条件，再说变化。把必要背景、原方法的困难、改动和效果串起来，篇幅随内容调整；普通文章、教程、事件不套论文框架。避免每段重开话题、只用“因此/核心是”掩盖缺失的关系。
story 可以为 null（尤其无需组织主线的极短原文）。提供文案时逐字保留，story 只能分析已有文字；缺失的解释放在问题中，不擅自补写。开场节选只规划已选内容，不强加完整故事结尾。填写主线不等于内容通过审核，审核以实际旁白与画面为准。'''


def check_understanding(job, plan, request, ai, *, frames=None, timeline=None, suffix='_understanding'):
    """Ask a fresh reader to reconstruct relations without seeing intended answers.

    This intentionally withholds story/explanation metadata: echoing the author's
    planned takeaway would not establish that the narration explains it.
    """
    if plan.get('story') is None:
        return None
    actual = [{'id': b['id'], 'narration': b['narration'],
               'shots': [{k: s.get(k) for k in ('kind', 'label', 'cue', 'source_id')}
                         for s in b['shots']]} for b in plan['blocks']]
    scope = '开场节选' if request.get('delivery_scope') == 'opening_sample' else '本片实际内容'
    prompt = ('你是第一次接触这个主题的观众。只根据下面实际旁白和所附画面，重建视频回答的问题和答案。'
        '没有提供作者预先填写的主线或预期结论，不要猜它想表达什么。只返回JSON。'
        'connections必须按每一个block各写一项，不遗漏、不重复。写出你实际理解到的关系：对象是什么，发生什么改变，为什么与前文相连。'
        '没有因果任务的背景、事件或教程不要强求因果；一般内容不强制论文结构。'
        'understood=false时必须在missing和同block的issues里说明具体缺口，不能同时声称无问题。关键关系缺失使本片主要结论无法理解或产生误导时记error，指出具体缺口和block_id；'
        '可简化的背景、个人文风偏好记warning。未解释的术语不能仅凭你的外部知识补全。'
        '不得因图像抽样推断连续动作或音色情绪，未提供音频不能声称听过。'
        f'范围为{scope}，不要求范围之外章节；保护原文不授权改稿，问题仍可提示用户。'
        '\n实际内容：' + json.dumps(actual, ensure_ascii=False) +
        '\n实际时间线：' + json.dumps(timeline or [], ensure_ascii=False))
    result = ai(prompt, frames or [], job/'analysis', request, UNDERSTANDING_SCHEMA, suffix)
    from jsonschema import validate
    validate(result, UNDERSTANDING_SCHEMA)
    ids = {b['id'] for b in plan['blocks']}
    checked=[c['block_id'] for c in result['connections']]
    if len(checked)!=len(ids) or set(checked)!=ids:
        raise ValueError('理解检查未逐段完整覆盖，或引用了不存在、重复的段落。')
    if any(i['block_id'] and i['block_id'] not in ids for i in result['issues']):
        raise ValueError('理解检查的问题引用了不存在的段落。')
    for connection in result['connections']:
        if not connection['understood'] and (not connection['missing'].strip()
                or not any(i['block_id']==connection['block_id'] for i in result['issues'])):
            raise ValueError('理解检查声称未理解，却没有提供对应问题，须重新审核。')
    result.update(status='completed', inspection='narration + supplied frames; no auditory listening',
                  version=STORYBOARD_VERSION)
    return result
