"""Editable explanation intent, separate from narration and source evidence.

The schema validates planning metadata, not scientific accuracy or aesthetics.
Legacy blocks may omit ``explanation`` or store null. Nothing here rewrites a
script, selects a source, renders labels, or claims a reference video was seen.
"""
from collections.abc import Mapping

from studio_story import _route


DIRECTOR_POLICY_VERSION = 1
VISUAL_STRATEGIES = ('demonstration', 'comparison', 'process', 'timeline', 'data', 'context')
_TEXT_LIMITS = {'question': 240, 'takeaway': 320, 'continuity': 240}
_FIELDS = ('question', 'takeaway', 'visual_strategy', 'continuity')
EXPLANATION_SCHEMA = {
    'anyOf': [
        {'type': 'null'},
        {
            'type': 'object',
            'properties': {
                'question': {'type': 'string', 'minLength': 1, 'maxLength': 240},
                'takeaway': {'type': 'string', 'minLength': 1, 'maxLength': 320},
                'visual_strategy': {'type': 'string', 'enum': list(VISUAL_STRATEGIES)},
                'continuity': {'type': 'string', 'maxLength': 240},
            },
            'required': list(_FIELDS),
            'additionalProperties': False,
        },
    ]
}


def normalize_explanation(value):
    """Validate and return independent metadata; None is the legacy default."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError('explanation 须为对象或 null。')
    if set(value) != set(_FIELDS):
        raise ValueError('explanation 需要且仅接受 question、takeaway、visual_strategy、continuity 四个字段。')
    result = {}
    for field in _FIELDS:
        text = value[field]
        if not isinstance(text, str):
            raise ValueError(f'explanation.{field} 须为文本。')
        if field == 'visual_strategy':
            if text not in VISUAL_STRATEGIES:
                raise ValueError('explanation.visual_strategy 须为 ' + '、'.join(VISUAL_STRATEGIES) + '。')
        else:
            if len(text) > _TEXT_LIMITS[field]:
                raise ValueError(f'explanation.{field} 不得超过 {_TEXT_LIMITS[field]} 个字符。')
            text = text.strip()
            if field != 'continuity' and not text:
                raise ValueError(f'explanation.{field} 不能为空。')
        result[field] = text
    return result


def validate_explanation(value):
    """Check the optional field without mutating the supplied block or text."""
    normalize_explanation(value)


def director_instruction(request, stage='plan'):
    """Return bounded, provider-neutral guidance for planning or review."""
    if stage not in ('plan', 'review'):
        raise ValueError('分镜解释阶段须为 plan 或 review。')
    mode, content = _route(request)
    parts = [
        '分镜解释：每段可用 explanation 记录一个具体观众问题 question、看完应理解的事实或关系 takeaway、主要视觉任务 visual_strategy，以及与相邻段落共享的对象、例子或逻辑联系 continuity（无须衔接时可为空）。这些是可编辑规划信息，不是旁白、证据或屏幕标签；不能靠填满字段证明讲清楚。',
        'visual_strategy 按本段内容选：demonstration 展示已观察的行为；comparison 在说明比较条件的前提下展示差别；process 解释实际步骤或作用关系；timeline 说明事件先后；data 解读指标、趋势与比较条件；context 交代身份、场景或必要背景。它不强制某种镜头类型、固定章节或视频使用比例。',
        '先核对可用来源，再把问题落实到具体 source_id、镜头与旁白 cue。讲行为时检查已获取的相关原片是否比静图更能展示动作；讲机制、比较或结果时选能说明相应关系的素材。下载过不等于看过，演示不能代替统计证据，概念箭头不能冒充实测因果。未使用合适素材时说明具体原因，缺失或未核实的画面不得编造。',
        '一段包含不同视觉任务时，按内容切换演示、过程或数据视图；不要让一张相关曲线承载它并未解释的动作和机制。保留相邻镜头的对象、方向与术语，先让观众找到讨论对象，再突出所述变化；不为追求切换次数或装饰动效打断必要观察。',
        '需要原图定位再局部放大时，可给 page/image 镜头设置 visual_beats=[{cue,bbox,label}]：bbox 为相对完整原始页面或图片的 0～1 坐标，必须位于该镜头 bbox 裁图内，不是最终画布坐标；首个 cue 为空，后续 cue 须在本段旁白中唯一出现且按先后排列，并落在该镜头的旁白窗口内。不得与 focus 或 column_crop 同用。放大仍须保留解释所需坐标、图例和条件；label 只记具体对象，不加入“先讲清”等制作提示。',
    ]
    if mode == 'protected':
        parts.append('用户文案受保护：逐字保留文字与顺序。explanation 只解释已有文案并指导镜头；不能为补足问题、衔接或视觉任务而改稿。缺口应定位到具体段落与来源，交给用户决定。')
    elif mode == 'source_order':
        parts.append('用户选择按原文顺序，explanation 与镜头沿该顺序组织；连续性设计不授权重排原文论述。')
    if content == 'general':
        parts.append('这是通用内容：按文章、事件或过程本身规划，不套论文、算法、实验或五章框架。')
    elif content == 'auto':
        parts.append('按实际内容辨认素材身份，PDF 不自动等于论文；文章和媒体沿自身内容规划，不虚构论文或实验。')
    if request.get('delivery_scope') == 'opening_sample':
        parts.append('这是开场节选，只规划和审核所选内容，不为补全论文或完整故事添加后续段落。')
    if stage == 'plan':
        parts.append('规划时让 explanation 与本段实际旁白和镜头一致，新增规划优先填写具体信息；不把观众问题机械改成问句开场，也不把 takeaway 重复写成文字卡。')
    else:
        parts.append('审核时对照实际旁白、所选来源和可见画面，检查问题是否得到回答、所述关系能否理解、镜头是否承担声明的任务；指出具体 block/shot 和证据。旧计划 explanation 缺失或为 null 不单独判错，字段完整也不代表通过。解释关系缺失造成事实错误、误导或关键关系无法理解时记 error；非关键静止时长、切换风格和构图偏好按实际影响记 warning，不按关键词、镜头比例或美感打分。明确的事实、来源及用户画面要求仍按原审核规则检查。')
    return '\n'.join(parts)
