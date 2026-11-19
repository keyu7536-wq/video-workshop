"""Persistent, shared candidate-generation quota for one substantive input.

Reviews do not spend quota. Every generation is reserved before the external
call, including failed/invalid generations. Automatic candidates are aliases
of the same input, so passing a repaired plan to another stage cannot reset it.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

import paper_video as pv
from studio_process import job_operation


MAX_REPAIR_ATTEMPTS = 3
_UNSET = object()


def generation_retryable(exc):
    """Retry only concrete output/validation feedback, never an unknown outage."""
    if isinstance(exc,(TimeoutError,ConnectionError,OSError)):return False
    text=str(exc).lower()
    if any(word in text for word in ('unavailable','service down','timeout','timed out',
                                     'rate limit','429','401','403','502','503','504',
                                     '网络','超时','额度','登录','不可用','connection','连接',
                                     'api key','api 模式','api 地址','api 模型','api_key',
                                     'unauthorized','forbidden','certificate','ssl',
                                     '粘贴内容过长')):return False
    if isinstance(exc,ValueError):return True
    # These are the model boundary's explicit successful-process/bad-payload
    # diagnostics. A generic nonzero model exit does not prove usable output.
    return isinstance(exc,RuntimeError) and any(message in text for message in (
        '本次结果不是可读取的 json 对象','本次结果必须是 json 对象',
        'invalid json','invalid model output','invalid schema output'))


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _identity(sources,request):
    from studio_plan_repair import repair_fingerprint
    return repair_fingerprint(None,sources,request,include_policy=False)


def _read(path):
    try:return pv.load_json(path)
    except (OSError,ValueError) as exc:
        raise ValueError(f'无法读取修订计数或候选记录，不能重置次数：{path.name}: {exc}') from exc


def _save(job,state):
    state['used']=len(state['attempts'])
    state['remaining']=max(0,MAX_REPAIR_ATTEMPTS-state['used'])
    folder=job/'analysis/repair_budgets'/state['budget_id']
    folder.mkdir(parents=True,exist_ok=True)
    pv.save_json(folder/'budget.json',state)
    pv.save_json(job/'analysis/repair_budget.json',state)


def _legacy(job,plan,sources,request):
    """Import only old attempts connected to the current plan's hash lineage.

    Old one-shot plan records have a complete input fingerprint. Older loose
    schema/timing/final outputs lack source/settings identity and cannot prove
    which input spent quota. Keep those artifacts, but do not charge a newly
    edited input merely because it still has the same plan hash.
    """
    from studio_plan_repair import repair_fingerprint
    entries=[]
    for path in sorted((job/'analysis/plan_repair').glob('*/attempt.json')):
        try:
            record=pv.load_json(path);before=pv.load_json(path.parent/'original_plan.json')
            if record.get('fingerprint')!=repair_fingerprint(before,sources,request):continue
            hashes={_hash(before)}
            if (path.parent/'candidate.json').is_file():hashes.add(_hash(pv.load_json(path.parent/'candidate.json')))
            entries.append({'stage':'plan','folder':str(path.parent),'hashes':hashes,'record':record})
        except (OSError,ValueError,TypeError,AttributeError):continue
    known={_hash(plan)};connected=[]
    while True:
        more=[entry for entry in entries if entry not in connected and entry['hashes']&known]
        if not more:break
        connected.extend(more)
        for entry in more:known.update(entry['hashes'])
    return connected,known


def _ensure(job,plan,sources,request):
    path=job/'analysis/repair_budget.json'
    identity=_identity(sources,request);plan_hash=_hash(plan)
    previous=_read(path) if path.is_file() else None
    if previous is not None:
        if (not isinstance(previous,dict) or previous.get('version')!=1
                or not isinstance(previous.get('attempts'),list)
                or not isinstance(previous.get('known_plan_hashes'),list)
                or not isinstance(previous.get('budget_id'),str)):
            raise ValueError('修订计数记录无效，不能以重新生成方式绕过次数上限。')
        if previous.get('input_fingerprint')==identity and plan_hash in previous['known_plan_hashes']:
            previous['remaining']=max(0,MAX_REPAIR_ATTEMPTS-len(previous['attempts']))
            return previous
    state={'version':1,'budget_id':uuid.uuid4().hex,'input_fingerprint':identity,
           'root_plan_hash':plan_hash,'known_plan_hashes':[plan_hash],
           'limit':MAX_REPAIR_ATTEMPTS,'used':0,'remaining':MAX_REPAIR_ATTEMPTS,'attempts':[]}
    if previous is None:
        entries,known=_legacy(job,plan,sources,request)
        state['known_plan_hashes']=sorted(known)
        for entry in entries:
            number=len(state['attempts'])+1
            record={**entry['record'],'budget_id':state['budget_id'],'number':number,
                    'stage':entry['stage'],'folder':entry['folder'],'migrated':True}
            # Keep all proven historical attempts even if the previous workflow
            # already exceeded the new limit; remaining quota is then zero.
            state['attempts'].append(record)
    _save(job,state)
    return state


def ensure_budget(job,plan,sources,request):
    """Return current input's ledger; only a user input change starts a new one."""
    job=Path(job)
    with job_operation(job,wait=True):return _ensure(job,plan,sources,request)


def register_plan(job,before,after,sources,request):
    """Register an ordinary host-generated plan without spending repair quota.

    The caller supplies the pre-generation plan (or None), so a random new
    initial draft is never mistaken for a user edit when resuming the workflow.
    """
    job=Path(job)
    with job_operation(job,wait=True):
        state=_ensure(job,before,sources,request)
        value=_hash(after)
        if value not in state['known_plan_hashes']:state['known_plan_hashes'].append(value)
        _save(job,state)
        return state


def budget_summary(job,plan,sources,request):
    """Read-only UI projection; never creates locks, folders or a new budget."""
    path=Path(job)/'analysis/repair_budget.json'
    if not path.is_file():return None
    try:
        state=_read(path)
        if (not isinstance(state,dict) or state.get('version')!=1
                or not isinstance(state.get('attempts'),list)
                or not isinstance(state.get('known_plan_hashes'),list)):
            raise ValueError('修订计数记录无效')
        stale=(state.get('input_fingerprint')!=_identity(sources,request)
               or _hash(plan) not in state['known_plan_hashes'])
        used=len(state['attempts'])
        return {'limit':MAX_REPAIR_ATTEMPTS,'used':used,'remaining':max(0,MAX_REPAIR_ATTEMPTS-used),
                'pending_review':any(item.get('status')=='review_unavailable'
                    and item.get('candidate_validated') is True for item in state['attempts']),
                'stale':stale,'attempts':[{key:item.get(key) for key in ('number','stage','status')}
                                        for item in state['attempts']]}
    except (ValueError,TypeError,AttributeError,OSError) as exc:
        return {'limit':MAX_REPAIR_ATTEMPTS,'used':None,'remaining':None,'pending_review':False,
                'stale':True,'attempts':[],'error':str(exc)}


def reserve_attempt(job,plan,sources,request,stage,feedback=None):
    """Spend one generation before calling a model; None means stop, not retry."""
    job=Path(job)
    if stage not in ('schema','plan','timing','final'):raise ValueError('未知修订阶段')
    with job_operation(job,wait=True):
        state=_ensure(job,plan,sources,request)
        if state['remaining']<=0:return None
        if any(item.get('status')=='review_unavailable' and item.get('candidate_validated') is True
               for item in state['attempts']):return None
        number=len(state['attempts'])+1
        folder=job/'analysis/repair_budgets'/state['budget_id']/f'{number:02}_{stage}'
        folder.mkdir(parents=True,exist_ok=True)
        record={'budget_id':state['budget_id'],'number':number,'stage':stage,'folder':str(folder),
                'status':'started','attempts':1,'review_attempts':0,'original_plan_hash':_hash(plan)}
        pv.save_json(folder/'original_plan.json',plan)
        if feedback is not None:pv.save_json(folder/'feedback.json',feedback)
        pv.save_json(folder/'attempt.json',record)
        state['attempts'].append(record)
        _save(job,state)
        return dict(record)


def finish_attempt(job,ticket,status,*,candidate=_UNSET,review=None,reason=None,candidate_validated=None):
    """Persist stage progress and artifacts without spending another generation."""
    job=Path(job)
    with job_operation(job,wait=True):
        state=_read(job/'analysis/repair_budget.json')
        if state.get('budget_id')!=ticket.get('budget_id'):
            raise ValueError('修订输入已经变化，不能将旧候选写入新输入。')
        index=ticket['number']-1
        if not 0<=index<len(state['attempts']):raise ValueError('修订次数记录不匹配')
        record=dict(state['attempts'][index]);folder=Path(record['folder'])
        if record.get('stage')!=ticket.get('stage'):raise ValueError('修订阶段不匹配')
        record['status']=status
        if candidate is not _UNSET:
            pv.save_json(folder/'candidate.json',candidate)
            record['candidate_plan_hash']=_hash(candidate)
            if record['candidate_plan_hash'] not in state['known_plan_hashes']:
                state['known_plan_hashes'].append(record['candidate_plan_hash'])
        if candidate_validated is not None:record['candidate_validated']=bool(candidate_validated)
        if status=='reviewing':record['review_attempts']=record.get('review_attempts',0)+1
        if review is not None:
            pv.save_json(folder/'candidate_review.json',review)
            pv.save_json(folder/f"candidate_review_{record.get('review_attempts',0):02}.json",review)
        if reason is not None:record['reason']=reason
        elif status in ('reviewing','accepted'):record.pop('reason',None)
        pv.save_json(folder/'attempt.json',record)
        state['attempts'][index]=record
        _save(job,state)
        return dict(record)


def pending_attempt(job,plan,sources,request,stage):
    """Return the saved same-input candidate whose external review was unavailable.

    Changed or missing artifacts fail closed rather than creating new quota or
    silently reviewing a different candidate under the old generation record.
    """
    job=Path(job)
    with job_operation(job,wait=True):
        state=_ensure(job,plan,sources,request)
        for record in reversed(state['attempts']):
            if (record.get('stage')!=stage or record.get('status')!='review_unavailable'
                    or record.get('candidate_validated') is not True):continue
            path=Path(record['folder'])/'candidate.json'
            candidate=_read(path)
            if record.get('candidate_plan_hash')!=_hash(candidate):
                raise ValueError('保存的修订候选 candidate 已改变，不能复用上次审核重试。')
            return dict(record)
        return None
