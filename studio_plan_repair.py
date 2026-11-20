"""Auditable plan repair within the workflow's shared generation budget."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import paper_video as pv


# Increment only when the repair instructions/constraints materially change.
# Policies remain part of old diagnostic fingerprints. Upgrading a policy never
# resets the shared generation quota or invalidates accepted story reviews.
REPAIR_POLICY_VERSION = 1


def _review_available(review):
    return (review.get('status') not in ('unavailable','incomplete','pending','skipped','failed')
            and review.get('review_complete') is not False
            and not review.get('unavailable_reason'))


def _accepted(review):
    return (_review_available(review) and review.get('ready') is True
            and not any(issue.get('severity')=='error' for issue in review.get('issues',[])))


def _json_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _with_proven_feedback(job, previous, issues, proof_path, summary):
    """Mark known errors pending without claiming a fresh full-plan audit."""
    from studio_revision import file_hash
    result=dict(previous)
    merged=list(previous.get('issues',[]));known={_json_hash(issue) for issue in merged}
    for issue in issues:
        if _json_hash(issue) not in known:
            merged.append(issue);known.add(_json_hash(issue))
    evidence={'path':str(proof_path.relative_to(job)).replace('\\','/'),
              'sha256':file_hash(proof_path)}
    provenance=list(result.get('_feedback_provenance',[]))
    if evidence not in provenance:provenance.append(evidence)
    result.update(ready=False,status='needs_changes',summary=summary,issues=merged,
                  original_plan_fully_rereviewed=False,
                  inspection='Prior review plus narrowly transferred, hash-bound errors; no new full-plan review.',
                  _feedback_provenance=provenance)
    result.pop('review_complete',None)
    result.pop('unavailable_reason',None)
    return result


def unchanged_block_feedback(job, original, candidate, candidate_review, previous, sources, folder):
    """Transfer only explicit errors about byte-equivalent logical blocks.

    Candidate narration (IDs, order and text) must match throughout. Neither
    global issues nor issues about edited blocks are applicable to the original.
    The caller retains the original plan and persists the returned pending review.
    """
    from studio_engine import plan_hash
    from studio_revision import file_hash
    if not isinstance(previous,dict) or not _review_available(candidate_review):return None
    original_hash=plan_hash(original);candidate_hash=plan_hash(candidate)
    if (previous.get('_plan_hash')!=original_hash
            or candidate_review.get('_plan_hash')!=candidate_hash):return None
    narration=lambda value:[(block.get('id'),block.get('narration')) for block in value.get('blocks',[])]
    if narration(original)!=narration(candidate):return None
    before={block.get('id'):block for block in original.get('blocks',[])}
    after={block.get('id'):block for block in candidate.get('blocks',[])}
    if len(before)!=len(original.get('blocks',[])) or len(after)!=len(candidate.get('blocks',[])):return None
    issues=[issue for issue in candidate_review.get('issues',[])
            if issue.get('severity')=='error' and issue.get('block_id')
            and issue['block_id'] in before and before[issue['block_id']]==after.get(issue['block_id'])]
    if not issues:return None
    review_path=folder/'rejected_review.json'
    if not review_path.is_file() or pv.load_json(review_path)!=candidate_review:return None
    blocks=list(dict.fromkeys(issue['block_id'] for issue in issues))
    source_files={}
    for source in sources:
        for key in ('file','preview'):
            name=source.get(key)
            if isinstance(name,str) and name and Path(name).is_file():source_files[name]=file_hash(Path(name))
    proof={'kind':'unchanged_candidate_blocks','original_plan_hash':original_hash,
           'candidate_plan_hash':candidate_hash,'candidate_review_sha256':file_hash(review_path),
           'narration_unchanged':True,'narration_sha256':_json_hash(narration(original)),
           'sources_sha256':_json_hash(sources),'source_files_sha256':source_files,
           'unchanged_blocks':[{'block_id':identifier,'sha256':_json_hash(before[identifier])} for identifier in blocks],
           'issues':issues,'scope':'Only these exact unchanged blocks; not a new full-original-plan review.'}
    # Later timing attempts may replace their working files. Preserve this
    # specific assessment and both compared plans under a content-derived path.
    snapshot=folder/'feedback'/_json_hash(proof);snapshot.mkdir(parents=True,exist_ok=True)
    for name,value in (('original_plan.json',original),('candidate.json',candidate),
                       ('candidate_review.json',candidate_review),('original_review.json',previous)):
        target=snapshot/name
        if not target.exists():pv.save_json(target,value)
    proof['snapshot']=str(snapshot.relative_to(job)).replace('\\','/')
    proof_path=snapshot/'proof.json';pv.save_json(proof_path,proof)
    pv.save_json(folder/'unchanged_block_feedback.json',proof)
    return _with_proven_feedback(job,previous,issues,proof_path,
        '修订候选审核在未改动段落发现明确错误，已转为原计划待处理问题；未对原计划重新做完整审核。')


def current_final_feedback(job, plan, request, previous):
    """Reuse a completed rejection only while all four reviewed versions match."""
    from studio_engine import plan_hash
    from studio_revision import file_hash,render_settings_hash
    path=job/'output/final_review.json';video=job/'output/video.mp4';project=job/'project.json'
    try:
        final=pv.load_json(path)
        if (final.get('ready') is not False or final.get('status')!='completed'
                or final.get('review_complete') is not True or not _review_available(final)
                or not final.get('reviews')
                or any(item.get('status')!='completed' for item in final['reviews'])):return previous
        issues=[issue for issue in final.get('issues',[]) if issue.get('severity')=='error']
        if not issues or not video.is_file() or not project.is_file():return previous
        versions={'plan_hash':plan_hash(plan),'settings_hash':render_settings_hash(request),
                  'video_sha256':file_hash(video),'project_sha256':file_hash(project)}
        if any(final.get(key)!=value for key,value in versions.items()):return previous
        review_hash=file_hash(path)
    except (OSError,ValueError,TypeError,AttributeError):return previous
    folder=job/'analysis/final_feedback'/review_hash;folder.mkdir(parents=True,exist_ok=True)
    if not (folder/'original_review.json').exists():pv.save_json(folder/'original_review.json',previous)
    pv.save_json(folder/'final_review.json',final)
    proof={'kind':'current_completed_final_review','source_review_sha256':review_hash,
           **versions,'issues':issues,
           'scope':'Errors from the already inspected current render; no new audit was run.'}
    proof_path=folder/'feedback.json';pv.save_json(proof_path,proof)
    result=_with_proven_feedback(job,previous,issues,proof_path,
        '当前版本已有完整成片审核指出明确错误，生成前先处理这些问题；本次尚未重新审核或编码。')
    pv.save_json(job/'review.json',result)
    return result


def repair_fingerprint(plan, sources, request, *, include_policy=True):
    """Exclude workflow toggles, credentials and access timestamps from identity."""
    from studio_display import DISPLAY_KEYS
    from studio_revision import file_hash
    from studio_story import STORY_POLICY_VERSION,normalize_framework
    keys=('brief','script','article','seconds','content_type','delivery_scope',
          'voice','rate','voice_style','backend','api_base','model',*DISPLAY_KEYS)
    settings={key:request.get(key) for key in keys}
    if request.get('rate')=='auto' and not str(request.get('voice','')).startswith('qwen:'):
        from studio_voice import normalize_base_rate
        settings['adaptive_base_rate']=normalize_base_rate(request.get('adaptive_base_rate'))
    from studio_duration import effective_duration_settings
    if effective_duration_settings(request)['duration_mode']=='adaptive':
        settings.pop('seconds',None)
        settings['duration_mode']='adaptive'
    settings['content_type']=request.get('content_type','auto')
    settings['story_framework']=normalize_framework(request.get('story_framework'))
    clean_sources=[{key:value for key,value in source.items()
                    if key not in ('accessed_at','downloaded_at')} for source in sources]
    files={}
    for source in sources:
        for key in ('file','preview'):
            value=source.get(key)
            if isinstance(value,str) and value and Path(value).is_file() and value not in files:
                files[value]=file_hash(Path(value))
    if not include_policy:
        # A shared generation budget follows actual user inputs, not software
        # policy upgrades or unused custom-canvas/target-duration memory.
        from studio_display import effective_display_settings
        for key in DISPLAY_KEYS:settings.pop(key,None)
        settings.update(effective_display_settings(request))
        settings.pop('seconds',None);settings.pop('duration_mode',None)
        settings.update(effective_duration_settings(request))
        for key in ('links','files'):settings[key]=request.get(key,[])
    payload={'plan':plan,'sources':clean_sources,'source_files':files,'settings':settings}
    if include_policy:
        payload.update(story_policy_version=STORY_POLICY_VERSION,repair_policy_version=REPAIR_POLICY_VERSION)
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def repair_plan_once(job, plan, sources, request, review):
    """Use the remaining shared quota; keep the original until a candidate passes.

    The historical function name remains for callers. Each rejected or invalid
    candidate supplies concrete feedback for the next generation. An external
    review failure suspends that same candidate and never triggers a new draft.
    """
    if request.get('auto_repair') is not True or _accepted(review) or not _review_available(review):
        return plan,review
    import studio_engine as engine
    from studio_process import redact
    from studio_repair_budget import (ensure_budget,reserve_attempt,finish_attempt,
                                      pending_attempt,generation_retryable)
    job=Path(job);analysis=job/'analysis'
    plan_path=analysis/'studio_plan.json';review_path=job/'review.json'
    original_plan=plan_path.read_bytes() if plan_path.is_file() else None
    original_review=review_path.read_bytes() if review_path.is_file() else None
    current=plan;feedback=review
    secrets=[request.get('key',''),request.get('api_key',''),
             os.environ.get('PAPER_VIDEO_API_KEY',''),os.environ.get('OPENAI_API_KEY','')]

    def detail(exc):return redact(f'{type(exc).__name__}: {exc}',secrets)[:1500]
    def remember(ticket):
        pv.save_json(analysis/'plan_repair_latest.json',ticket|{'repair_policy_version':REPAIR_POLICY_VERSION})
    def restore():
        for path,original in ((plan_path,original_plan),(review_path,original_review)):
            if original is not None:path.write_bytes(original)
            elif path.is_file():path.unlink()

    state=ensure_budget(job,plan,sources,request)
    reviewed={engine.plan_hash(plan):review}
    for item in state['attempts']:
        folder=Path(item['folder']);saved=folder/'candidate_review.json'
        if item.get('candidate_plan_hash') and saved.is_file():
            checked=pv.load_json(saved)
            if _review_available(checked):reviewed[item['candidate_plan_hash']]=checked
    try:pending=pending_attempt(job,plan,sources,request,'plan')
    except ValueError as exc:
        pv.log('  无法复用修订候选：'+detail(exc));return plan,review
    # An interrupted generation has spent its reservation. A later request uses
    # its saved concrete outcome, rather than resetting the original counter.
    history=[item for item in state['attempts'] if item.get('stage')=='plan']
    if history and pending is None:
        last=history[-1];folder=Path(last['folder'])
        if last.get('candidate_plan_hash') and (folder/'candidate.json').is_file():
            candidate=pv.load_json(folder/'candidate.json')
            if engine.plan_hash(candidate)==last['candidate_plan_hash']:current=candidate
        if (folder/'candidate_review.json').is_file():feedback=pv.load_json(folder/'candidate_review.json')
        elif last.get('reason'):
            feedback={'ready':False,'summary':last['reason'],'issues':[{'severity':'error','reason':last['reason']}]}

    while True:
        ticket=pending or reserve_attempt(job,plan,sources,request,'plan',feedback)
        if ticket is None:
            pv.log('【文稿与分镜】本输入已用完共享修订次数，或有候选正在等待恢复审核；保留当前计划和具体问题。')
            return plan,review
        resume=pending is not None;pending=None
        folder=Path(ticket['folder']);accepted=False;candidate=None
        phase='validation' if resume else 'generation'
        pv.save_json(folder/'original_review.json',review)
        pv.log('【文稿与分镜】只重新审核已保存候选，不生成新稿' if resume else
               f"【文稿与分镜】根据具体反馈生成修订候选（共享次数 {ticket['number']}/3）")
        try:
            if resume:
                candidate=pv.load_json(folder/'candidate.json')
            else:
                images=pv.load_json(analysis/'images.json') if (analysis/'images.json').is_file() else []
                prompt=engine.prompt_for(sources,images,request)
                prompt+='\n本次只对以下具体问题做最小修复。保留已正确的事实、主线和相关镜头；不能通过删去关键解释或事实限定来换取通过。'
                prompt+='根据实际来源修正错误文稿或不对应的镜头，无法核实时保留缺口，不得编造实验。若用户给定script，逐字及顺序保护仍最高优先，只能修分段和镜头，不能改稿。'
                prompt+='反馈中的画面建议须回到现有素材和渲染能力判断，不能把建议当成已存在的图片。非受保护的自动文案可把无法展示的自拟情节改为来源支持、画面能说明的具体例子，仍保留中心问题和必要解释。'
                prompt+='\n原始计划：'+json.dumps(plan,ensure_ascii=False)+'\n上一轮候选：'+json.dumps(current,ensure_ascii=False)
                prompt+='\n本轮需要处理的具体反馈：'+json.dumps(feedback,ensure_ascii=False)
                candidate=engine.call_ai(prompt,[Path(path) for _,path in images],analysis,request,
                                         engine.SCHEMA,f"_plan_repair_{ticket['budget_id'][:12]}_{ticket['number']}")
                ticket=finish_attempt(job,ticket,'generated',candidate=candidate,candidate_validated=False)
            phase='validation'
            engine.validate(candidate,sources,request.get('script','').strip())
            engine.validate_visual_policy(candidate,request,sources,job)
            ticket=finish_attempt(job,ticket,'validated',candidate=candidate,candidate_validated=True)
            candidate_hash=engine.plan_hash(candidate)
            if candidate_hash in reviewed and not _accepted(reviewed[candidate_hash]):
                candidate_review=reviewed[candidate_hash]
                ticket=finish_attempt(job,ticket,'repeated_candidate',review=candidate_review,
                    reason='候选与已完成审核的拒绝版本相同，不重复审核碰运气；需要实际修正具体问题。')
                feedback={**candidate_review,'repair_feedback':ticket['reason']}
                current=candidate;remember(ticket)
                continue
            phase='review'
            ticket=finish_attempt(job,ticket,'reviewing')
            candidate_review=engine.review_plan(job,candidate,sources,request)
            if not _review_available(candidate_review):
                ticket=finish_attempt(job,ticket,'review_unavailable',review=candidate_review,
                                      reason='审核暂未完成，保留同一候选供重新审核，不生成新稿。')
                remember(ticket);return plan,review
            reviewed[candidate_hash]=candidate_review
            if not _accepted(candidate_review):
                ticket=finish_attempt(job,ticket,'rejected',review=candidate_review)
                feedback=candidate_review;current=candidate;remember(ticket)
                continue
            phase='commit'
            pv.save_json(plan_path,candidate);pv.save_json(review_path,candidate_review)
            ticket=finish_attempt(job,ticket,'accepted',review=candidate_review)
            accepted=True;remember(ticket)
            return candidate,candidate_review
        except Exception as exc:
            status={'generation':'generation_failed','validation':'invalid_candidate',
                    'review':'review_unavailable','commit':'save_failed'}[phase]
            message=detail(exc)
            ticket=finish_attempt(job,ticket,status,reason=message,
                                  candidate_validated=False if phase=='validation' else None)
            if phase=='review':
                pv.save_json(folder/f"candidate_review_failure_{ticket['review_attempts']:02}.json",
                             {'status':'unavailable','reason':message})
            remember(ticket)
            pv.log('  修订候选未通过，保留原计划：'+message[:250])
            if phase not in ('generation','validation') or not generation_retryable(exc):return plan,review
            if candidate is not None:current=candidate
            feedback={'ready':False,'summary':message,'issues':[{'severity':'error','reason':message}]}
        finally:
            if not accepted:restore()
