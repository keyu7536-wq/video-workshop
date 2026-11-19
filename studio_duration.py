"""Planning duration choices, independent of speech speed and measured timing."""
from __future__ import annotations

import hashlib
import json
import re


def normalize_duration_mode(value='target'):
    if value not in ('target','adaptive'):
        raise ValueError('时长模式须为目标时长或自适应。')
    return value


def target_seconds(value=100):
    # Accept legacy numeric form strings, without truncating fractions or bools.
    if isinstance(value,str) and re.fullmatch(r'\d+',value):value=int(value)
    if type(value) is not int or not 30<=value<=600:
        raise ValueError('目标时长需为30–600秒之间的整数。')
    return value


def effective_duration_settings(request):
    mode=normalize_duration_mode(request.get('duration_mode','target'))
    # In adaptive mode a provided seconds field is only valid target-mode memory.
    seconds=target_seconds(request.get('seconds',100))
    return {'duration_mode':mode,**({'seconds':seconds} if mode=='target' else {})}


def duration_settings(request,data):
    """Validate a deliberate settings edit without changing any narration."""
    changes={key:data[key] for key in ('duration_mode','seconds') if key in data}
    result={**request,**changes}
    effective=effective_duration_settings(result)
    if 'seconds' in changes:result['seconds']=target_seconds(changes['seconds'])
    if changes and effective!=effective_duration_settings(request):
        # This makes edits to legacy reviews detectable without globally
        # expiring unchanged target-mode tasks or their already encoded videos.
        result['_duration_revision']=hashlib.sha256(
            json.dumps(effective,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    return result


def duration_review_stale(review,request):
    effective=effective_duration_settings(request)
    if '_duration_settings' in review:
        return review['_duration_settings']!=effective
    return effective['duration_mode']=='adaptive' or request.get('_duration_revision') is not None


def duration_instruction(request,stage='plan',estimated_cps=None):
    settings=effective_duration_settings(request)
    if settings['duration_mode']=='adaptive':
        policy=('时长模式：自适应，没有固定秒数目标。根据内容重点、必要解释、受众和可用素材安排篇幅，'
                '讲完重点即可，不为填满时长添加重复内容，不因默认秒数删去必要信息。')
    else:
        policy=f"时长模式：目标约{settings['seconds']}秒，是规划预算而非硬上限。"
        if stage=='plan' and estimated_cps is not None:
            policy+=f'未给文案时按所选语速粗估每秒{estimated_cps:.1f}汉字，留出理解和完整动作时间。'
    policy+='实际片长由真实配音和完整镜头决定；保留用户给定文案，不删句，不靠拉慢或加速配音、空镜或重复画面凑时长。'
    if stage=='review':
        policy+='仅在用户创作要求明确给出硬上限时按该上限核查；否则不单凭秒数偏离判error或ready=false，仍检查真实静默、拖沓和信息过密。'
    return policy
