"""One auditable plan-repair attempt per substantive input version."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import paper_video as pv


# Increment only when the repair instructions/constraints materially change.
# This permits one new attempt after a constraint correction without rerunning
# accepted story reviews or rerolling unchanged candidates on every click.
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


def repair_fingerprint(plan, sources, request):
    """Exclude workflow toggles, credentials and access timestamps from identity."""
    from studio_display import DISPLAY_KEYS
    from studio_revision import file_hash
    from studio_story import STORY_POLICY_VERSION,normalize_framework
    keys=('brief','script','article','seconds','content_type','delivery_scope',
          'voice','rate','voice_style','backend','api_base','model',*DISPLAY_KEYS)
    settings={key:request.get(key) for key in keys}
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
    payload={'plan':plan,'sources':clean_sources,'source_files':files,'settings':settings,
             'story_policy_version':STORY_POLICY_VERSION,
             'repair_policy_version':REPAIR_POLICY_VERSION}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def repair_plan_once(job, plan, sources, request, review):
    """Return the accepted candidate and review, or the unchanged original pair.

    Generation is claimed once. A valid candidate whose review was unavailable
    may be reviewed again on a later invocation, without generating a new one.
    Rejected/invalid candidates cannot use that path. Active plan/review bytes
    are restored on failure, and every invocation performs at most one review.
    """
    if request.get('auto_repair') is not True or _accepted(review) or not _review_available(review):
        return plan,review
    import studio_engine as engine
    from studio_process import redact
    job=Path(job);analysis=job/'analysis'
    fingerprint=repair_fingerprint(plan,sources,request)
    folder=analysis/'plan_repair'/fingerprint
    folder.mkdir(parents=True,exist_ok=True)
    record_path=folder/'attempt.json'
    record={'fingerprint':fingerprint,'status':'started','attempts':1,'review_attempts':0,
            'repair_policy_version':REPAIR_POLICY_VERSION}
    resume_review=False
    # Exclusive creation also prevents two callers from spending another attempt.
    try:
        with record_path.open('x',encoding='utf-8') as stream:
            json.dump(record,stream,ensure_ascii=False,indent=2)
    except FileExistsError:
        record=pv.load_json(record_path)
        resume_review=(record.get('status')=='review_unavailable'
                       and record.get('candidate_validated') is True)
        if not resume_review:
            pv.log('【文稿与分镜】本版计划已尝试自动返修，保留审核结果；修改资料、文稿或设置后可再尝试。')
            return plan,review
    plan_path=analysis/'studio_plan.json';review_path=job/'review.json'
    original_plan=plan_path.read_bytes() if plan_path.is_file() else None
    original_review=review_path.read_bytes() if review_path.is_file() else None
    if not resume_review:
        pv.save_json(folder/'original_plan.json',plan)
        pv.save_json(folder/'original_review.json',review)
    accepted=False;candidate=None
    phase='validation' if resume_review else 'generation'
    pv.log('【文稿与分镜】重新审核已保存的修订候选，不再生成新稿' if resume_review
           else '【文稿与分镜】针对计划审核的具体问题自动返修一次')
    try:
        if resume_review:
            candidate=pv.load_json(folder/'candidate.json')
            if (record.get('fingerprint')!=fingerprint
                    or record.get('candidate_plan_hash')!=engine.plan_hash(candidate)):
                raise ValueError('保存的修订候选或输入指纹已改变，不能复用上次审核重试。')
        else:
            images=pv.load_json(analysis/'images.json') if (analysis/'images.json').is_file() else []
            prompt=engine.prompt_for(sources,images,request)
            prompt+='\n本次只对以下计划审核指出的具体问题做最小修复。保留已正确的事实、主线和相关镜头；不能通过删去关键解释或事实限定来换取通过。'
            prompt+='根据实际来源修正错误文稿或不对应的镜头，无法核实时保留缺口，不得编造实验。若用户给定script，逐字及顺序保护仍最高优先，只能修分段和镜头，不能改稿。'
            prompt+='\n当前计划：'+json.dumps(plan,ensure_ascii=False)+'\n具体审核：'+json.dumps(review,ensure_ascii=False)
            candidate=engine.call_ai(prompt,[Path(path) for _,path in images],analysis,request,
                                     engine.SCHEMA,'_plan_repair_'+fingerprint[:12])
            pv.save_json(folder/'candidate.json',candidate)
        phase='validation'
        engine.validate(candidate,sources,request.get('script','').strip())
        engine.validate_visual_policy(candidate,request)
        pv.save_json(folder/'candidate.json',candidate)
        record.update(candidate_validated=True,candidate_plan_hash=engine.plan_hash(candidate))
        phase='review'
        record['review_attempts']=record.get('review_attempts',0)+1
        record['status']='reviewing';record.pop('reason',None)
        pv.save_json(record_path,record)
        candidate_review=engine.review_plan(job,candidate,sources,request)
        pv.save_json(folder/'candidate_review.json',candidate_review)
        pv.save_json(folder/f"candidate_review_{record['review_attempts']:02}.json",candidate_review)
        if not _accepted(candidate_review):
            record.update(status='rejected' if _review_available(candidate_review) else 'review_unavailable',
                          reason='修订候选未获完整通过，保留原计划与原审核；审核不可用不视为内容错误。')
            return plan,review
        phase='commit'
        pv.save_json(plan_path,candidate)
        pv.save_json(review_path,candidate_review)
        accepted=True
        record.update(status='accepted',accepted_plan_hash=engine.plan_hash(candidate))
        return candidate,candidate_review
    except Exception as exc:
        secrets=[request.get('key',''),request.get('api_key',''),
                 os.environ.get('PAPER_VIDEO_API_KEY',''),os.environ.get('OPENAI_API_KEY','')]
        detail=redact(f'{type(exc).__name__}: {exc}',secrets)[:1500]
        status={'generation':'generation_failed','validation':'invalid_candidate',
                'review':'review_unavailable','commit':'save_failed'}[phase]
        record.update(status=status,reason=detail)
        if phase=='validation':record['candidate_validated']=False
        if phase=='review':
            pv.save_json(folder/f"candidate_review_failure_{record['review_attempts']:02}.json",
                         {'status':'unavailable','reason':detail})
        pv.log('  自动计划返修未通过，保留原计划与审核：'+detail[:250])
        return plan,review
    finally:
        if not accepted:
            for path,original in ((plan_path,original_plan),(review_path,original_review)):
                if original is not None:path.write_bytes(original)
                elif path.is_file():path.unlink()
        pv.save_json(record_path,record)
        pv.save_json(analysis/'plan_repair_latest.json',record)
