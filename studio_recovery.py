"""Bounded program-side recovery for measured visual-duration shortages."""
from pathlib import Path
import json
import os
import uuid

import paper_video as pv


class _ProtectedTimingError(ValueError):
    """A timing-only revision must not rewrite speech or remove protected action."""


def _timing_prompt(engine,sources,images,request,plan,diagnostic,measured_plan,rejected_review):
    prompt=engine.prompt_for(sources,images,request)
    prompt+='\n本次只修正镜头长度不足，不能改写、删减、拆分或重排任何段落的id和narration。保留已经正确的镜头与事实。'
    prompt+='诊断blocks的cue_candidates提供原文中的唯一短语与真实词边界start，cue_time_basis=block_local表示本段起点后的秒数，不是全片秒数。列表最多48条、每条最多24字，长段只是有界采样，不表示所有可用切点，也不表示每一处在语义上都适合切镜。不要按字数或duration权重重新估算已有配音时间。'
    prompt+='遇到complete_clip_exceeds_cue_window，优先在语义允许的更早实测cue切入同一动作视频，保持其source_id、in、duration、keep_full和后续关键演示切点。用后一个实际cue时间减去候选start检查完整取段能否放下，并核对前一镜头及本段其他完整动作仍有足够时间。修改cue只改变计划切点，不会重写或挪动原始配音；不能指望渲染器暗中提前cue。'
    prompt+='旁白若要求观看奔跑、抓取、开合等实际运动或动作变化，必须保留对应运动画面，不能换成同主体静图、论文截图或概念示意冒充动作已经展示。这个判断依据完整旁白和素材实际内容，不是见到某个动词就机械判断。解释机制或背景的余量才可用现有相关图、论文图或准确原理示意承接；不要慢放、循环演示、长时间冻结原片，不能生成大段文字卡凑时长。'
    prompt+='keep_full是必须保留完整取段的要求：本次时序修复必须在原段保留每一处原source_id、原in、不少于原duration且keep_full=true的镜头，不能整段换成静图/其他视频，不能移到别段，也不能用同一个候选镜头抵掉多个原取段。允许改变其cue或补充其他镜头。需要换素材由用户明确修改或独立内容修订阶段决定。若没有既语义对应又容得下完整动作的切点，保留原镜头和明确问题，让此次修复失败；不能改旁白、删除正在讲述的动作或冒充已解决。'
    prompt+='原片不够且没有可信解释画面时保留明确问题，不得编造实验或改写旁白掩盖缺口。'
    prompt+='\n当前完整计划：'+json.dumps(plan,ensure_ascii=False)+'\n实测缺口：'+json.dumps(diagnostic,ensure_ascii=False)
    if measured_plan!=plan:
        prompt+='\n上述实测诊断对应以下计划；当前候选尚未重新编码，不得把旧秒数说成候选实测结果：'+json.dumps(measured_plan,ensure_ascii=False)
    if rejected_review is not None:
        prompt+='\n上一候选的计划审核（不是成片反馈）：'+json.dumps(rejected_review,ensure_ascii=False)
    return prompt


def _validate_preserved(engine,before,after,sources,request):
    blocks=after.get('blocks') if isinstance(after,dict) else None
    if not isinstance(blocks,list) or any(not isinstance(block,dict) for block in blocks):
        engine.validate(after,sources,request.get('script','').strip())
    if [(b.get('id'),b.get('narration')) for b in blocks]!=[(b['id'],b['narration']) for b in before['blocks']]:
        raise _ProtectedTimingError('自动补镜头修改了原文稿，已保留原计划。请在分镜页面补充相关画面后重试。')
    engine.validate(after,sources,request.get('script','').strip())
    engine.validate_visual_policy(after,request)
    for original_block,candidate_block in zip(before['blocks'],after['blocks']):
        remaining=[shot for shot in candidate_block['shots'] if shot['kind']=='video']
        for original in original_block['shots']:
            if original['kind']!='video' or not original.get('keep_full'):continue
            match=next((index for index,shot in enumerate(remaining)
                        if shot['source_id']==original['source_id']
                        and abs(shot['in']-original['in'])<.01
                        and shot.get('keep_full') is True
                        and shot['duration']>=original['duration']-.01),None)
            if match is None:
                raise _ProtectedTimingError('自动补镜头移除或截短了要求完整保留的动作，已保留原计划；只能调整切点或增加解释镜头。')
            remaining.pop(match)


def _render_diagnostic(job,engine):
    from studio_revision import file_hash
    path=job/'output/timing_recovery.json'
    if path.is_file():
        (job/'analysis/timing_recovery_previous.json').write_bytes(path.read_bytes())
        path.unlink()
    try:
        engine.render(job)
        return None,None
    except RuntimeError as error:
        try:diagnostic=pv.load_json(path)
        except (OSError,ValueError):raise error
        if (not isinstance(diagnostic,dict) or diagnostic.get('status')!='needs_more_visuals'
                or diagnostic.get('code')!='insufficient_visual_duration'
                or diagnostic.get('project_sha256')!=file_hash(job/'project.json')
                or not diagnostic.get('issues')):
            raise error
        return diagnostic,error


def render_with_recovery(job,request,plan,sources):
    import studio_engine as engine
    from studio_plan_repair import _accepted,_review_available,unchanged_block_feedback
    from studio_repair_budget import ensure_budget,reserve_attempt,finish_attempt,pending_attempt,generation_retryable
    from studio_repair_checkpoint import capture_checkpoint,restore_checkpoint
    job=Path(job);folder=job/'analysis/timing_recovery';folder.mkdir(parents=True,exist_ok=True)
    enabled=request.get('auto_repair',True)
    budget=ensure_budget(job,plan,sources,request) if enabled else None
    pending=pending_attempt(job,plan,sources,request,'timing') if enabled else None
    checkpoint=capture_checkpoint(job,folder/'checkpoints'/uuid.uuid4().hex)
    old_review=(job/'review.json').read_bytes() if (job/'review.json').is_file() else None
    feedback=None;committed=False;current=plan;measured_plan=plan;rejected_review=None
    protected_plan=plan
    rejected_cache={};invalid_feedback=None
    # Reuse only completed plan rejections of these exact timing candidates.
    # Final-render feedback has a different inspection scope and is not read here.
    for previous in (budget or {}).get('attempts',[]):
        if previous.get('stage')!='timing' or previous.get('status')!='rejected':continue
        prior_folder=Path(previous['folder'])
        candidate=pv.load_json(prior_folder/'candidate.json')
        review=pv.load_json(prior_folder/'candidate_review.json')
        candidate_hash=engine.plan_hash(candidate)
        if (candidate_hash==previous.get('candidate_plan_hash')
                and _review_available(review) and not _accepted(review)):
            rejected_cache[candidate_hash]=review
    images=pv.load_json(job/'analysis/images.json') if (job/'analysis/images.json').is_file() else []
    try:
        if pending:
            prior=Path(pending['folder'])
            current=pv.load_json(prior/'plan_before.json')
            if engine.plan_hash(current)!=pending.get('original_plan_hash'):
                raise ValueError('保存的时序保护基线已改变，不能复用候选审核。')
            # An outer final repair may have rolled the active files back to an
            # older draft. Timing protects its own hash-bound input, while the
            # checkpoint above still protects the currently usable root files.
            protected_plan=current
            measured_plan=pv.load_json(prior/'measured_plan.json')
            diagnostic=pv.load_json(prior/'diagnostic.json')
            render_error=RuntimeError('时序修订候选仍需复核。')
        else:
            diagnostic,render_error=_render_diagnostic(job,engine)
            if diagnostic is None:committed=True;return plan
            if not enabled:raise render_error
        pv.save_json(folder/'plan_before.json',protected_plan)
        if old_review is not None:(folder/'original_review.json').write_bytes(old_review)
        while True:
            ticket=pending or reserve_attempt(job,current,sources,request,'timing',feedback=diagnostic)
            if ticket is None:
                if rejected_review is not None:
                    raise ValueError('自动补充的镜头未通过内容审核，共享返修预算已用尽，已保留原文稿与分镜。')
                raise render_error
            attempt=Path(ticket['folder']);attempt.mkdir(parents=True,exist_ok=True)
            phase='validation' if pending else 'generation'
            fixed=None
            try:
                if pending:
                    fixed=pv.load_json(attempt/'candidate.json')
                    pv.log('【成片与导出】重新审核已保存的时序候选，不重复生成')
                else:
                    pv.log(f"【成片与导出】按实测配音补充镜头（共享返修 {ticket['number']}/3）")
                    for name,value in (('plan_before.json',current),('measured_plan.json',measured_plan),
                                       ('diagnostic.json',diagnostic)):
                        pv.save_json(attempt/name,value)
                    prompt=_timing_prompt(engine,sources,images,request,current,diagnostic,measured_plan,rejected_review)
                    if invalid_feedback is not None:
                        prompt+='\n上一轮回复未通过结构校验，请按具体错误修正，不得改变受保护的旁白和完整动作：'+json.dumps(invalid_feedback,ensure_ascii=False)
                    fixed=engine.call_ai(prompt,[Path(path) for _,path in images],job/'analysis',request,
                                         engine.SCHEMA,'_timing_repair')
                phase='validation'
                finish_attempt(job,ticket,'validating',candidate=fixed,candidate_validated=False)
                _validate_preserved(engine,current,fixed,sources,request)
                _validate_preserved(engine,protected_plan,fixed,sources,request)
                pv.save_json(folder/'diagnostic.json',diagnostic)
                pv.save_json(folder/'candidate.json',fixed)
                fixed_hash=engine.plan_hash(fixed)
                phase='review'
                if fixed_hash in rejected_cache:
                    review=rejected_cache[fixed_hash]
                else:
                    finish_attempt(job,ticket,'reviewing',candidate=fixed,candidate_validated=True)
                    review=engine.review_plan(job,fixed,sources,request)
                if not _review_available(review):
                    finish_attempt(job,ticket,'review_unavailable',candidate=fixed,review=review,candidate_validated=True)
                    raise RuntimeError('时序修订候选审核暂不可用，已保存候选；再次生成时仅重审，不重复消耗返修次数。')
                if not _accepted(review):
                    rejected_cache[fixed_hash]=review
                    finish_attempt(job,ticket,'rejected',candidate=fixed,review=review,candidate_validated=True)
                    pv.save_json(folder/'rejected_review.json',review)
                    feedback=unchanged_block_feedback(job,plan,fixed,review,
                        json.loads(old_review) if old_review is not None else None,sources,folder) or feedback
                    current=fixed;rejected_review=review;pending=None
                    continue
                phase='render'
                pv.save_json(job/'analysis/studio_plan.json',fixed)
                pv.save_json(job/'review.json',review)
                engine.export(job,fixed,sources,request)
                diagnostic,render_error=_render_diagnostic(job,engine)
                if diagnostic is None:
                    finish_attempt(job,ticket,'accepted',candidate=fixed,review=review,candidate_validated=True)
                    pv.save_json(folder/'plan_after.json',fixed)
                    committed=True
                    return fixed
                finish_attempt(job,ticket,'timing_failed',candidate=fixed,review=review,candidate_validated=True)
                pv.save_json(attempt/'diagnostic_after.json',diagnostic)
                current=fixed;measured_plan=fixed;rejected_review=None;pending=None
            except Exception as exc:
                from studio_process import redact
                status={'generation':'generation_failed','validation':'invalid_candidate',
                        'review':'review_unavailable','render':'render_failed'}[phase]
                secrets=[request.get('key',''),request.get('api_key',''),
                         os.environ.get('PAPER_VIDEO_API_KEY',''),os.environ.get('OPENAI_API_KEY','')]
                finish_attempt(job,ticket,status,reason=redact(str(exc),secrets)[:1500],
                               candidate_validated=(phase in ('review','render')))
                if (phase in ('generation','validation') and not isinstance(exc,_ProtectedTimingError)
                        and generation_retryable(exc)):
                    invalid_feedback={'error':redact(str(exc),secrets)[:1500],'candidate':fixed}
                    render_error=exc;rejected_review=None;pending=None
                    continue
                raise
    finally:
        if not committed:
            restore_checkpoint(checkpoint)
            if feedback is not None:pv.save_json(job/'review.json',feedback)
