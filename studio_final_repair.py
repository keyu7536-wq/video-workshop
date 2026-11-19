"""Revise measured final-video problems within the task's shared budget."""
from pathlib import Path
import json

import paper_video as pv


def _saved_rejections(job,plan,sources,request):
    """Reuse only completed, input-bound rejections of the exact saved candidate."""
    import studio_engine as engine
    from studio_plan_repair import _accepted,_review_available
    from studio_repair_budget import ensure_budget
    from studio_revision import file_hash,render_settings_hash
    rejected={}
    for ticket in ensure_budget(job,plan,sources,request)['attempts']:
        if (ticket.get('stage')!='final' or ticket.get('status')!='rejected'
                or ticket.get('candidate_validated') is not True):continue
        folder=Path(ticket['folder'])
        try:
            candidate=pv.load_json(folder/'candidate.json')
            digest=engine.plan_hash(candidate)
            review=pv.load_json(folder/'candidate_review.json')
            if (digest!=ticket.get('candidate_plan_hash') or not _review_available(review)
                    or _accepted(review)):continue
            if review.get('_plan_hash')==digest:
                rejected[digest]=review
            elif (review.get('plan_hash')==digest and review.get('status')=='completed'
                    and review.get('review_complete') is True
                    and review.get('settings_hash')==render_settings_hash(request)):
                # A rendered rejection is bound to the archived encoded output,
                # not whichever video currently occupies the active job path.
                rendered=folder/'rendered'
                if (review.get('video_sha256')==file_hash(rendered/'output/video.mp4')
                        and review.get('project_sha256')==file_hash(rendered/'project.json')
                        and engine.plan_hash(pv.load_json(rendered/'analysis/studio_plan.json'))==digest):
                    rejected[digest]=review
        except (OSError,ValueError,TypeError,AttributeError):continue
    return rejected


def repair_final_video(job,request,plan,sources,final):
    import studio_engine as engine
    from studio_delivery import final_review
    from studio_plan_repair import _accepted,_review_available
    from studio_repair_budget import reserve_attempt,finish_attempt,pending_attempt
    from studio_repair_checkpoint import capture_checkpoint,restore_checkpoint
    from studio_recovery import render_with_recovery
    from studio_process import redact
    from studio_revision import file_hash
    if not request.get('auto_repair',True) or _accepted(final) or not _review_available(final):
        return plan,final
    job=Path(job);analysis=job/'analysis'
    original_plan=plan;original_final=final
    working=plan;feedback=final;rejected_reviews=_saved_rejections(job,plan,sources,request);last_reason=''
    round_number=int(final.get('round',1))
    images=pv.load_json(analysis/'images.json') if (analysis/'images.json').is_file() else []
    while True:
        pending=pending_attempt(job,original_plan,sources,request,'final')
        ticket=pending or reserve_attempt(job,original_plan,sources,request,'final',feedback=feedback)
        if ticket is None:break
        folder=Path(ticket['folder']);folder.mkdir(parents=True,exist_ok=True)
        checkpoint=capture_checkpoint(job,folder/'before')
        candidate=None;candidate_review=None;accepted=False;validated=False;phase='generation';stop=False
        try:
            if pending:
                candidate=pv.load_json(folder/'candidate.json');validated=True
                saved=pv.load_json(folder/'resume.json')
                phase=saved['phase']
                pv.log(f"【成片与导出】继续复核第 {ticket['number']}/3 次返修，复用已保存候选")
                if phase=='final_review':
                    restore_checkpoint(pv.load_json(folder/'rendered_checkpoint.json'))
                    if any(file_hash(job/name)!=digest for name,digest in saved['hashes'].items()):
                        raise ValueError('已保存的返修成片版本发生变化，不能复用审核。')
            else:
                pv.log(f"【成片与导出】自动返修 {ticket['number']}/3：根据上一轮具体问题修订")
                prompt=engine.prompt_for(sources,images,request)
                prompt+='\n对以下当前候选做最小修正，解决上一轮具体错误。保留用户文案与正确事实；不能删去关键解释、图例或比较条件来换取通过。'
                prompt+='若之前候选被拒，必须处理其明确问题；不能交回完全相同的计划要求重新抽签审核。'
                prompt+='\n当前候选：'+json.dumps(working,ensure_ascii=False)+'\n上一轮反馈：'+json.dumps(feedback,ensure_ascii=False)
                suffix='_render_repair' if ticket['number']==1 else f"_render_repair_{ticket['number']}"
                candidate=engine.call_ai(prompt,[Path(item[1]) for item in images],analysis,request,engine.SCHEMA,suffix)
                phase='validation'
                engine.validate(candidate,sources,request.get('script','').strip())
                engine.validate_visual_policy(candidate,request);validated=True
                digest=engine.plan_hash(candidate)
                if digest==engine.plan_hash(working) or digest in rejected_reviews:
                    reason='修订候选与已拒绝版本完全相同，尚未处理上一轮问题。'
                    prior=rejected_reviews.get(digest,feedback)
                    finish_attempt(job,ticket,'rejected',candidate=candidate,review=prior,reason=reason,candidate_validated=True)
                    last_reason=reason;working=candidate;feedback={**prior,'repair_error':reason};continue
                phase='plan_review'
            if phase!='final_review':
                pv.save_json(folder/'candidate.json',candidate)
                pv.save_json(folder/'resume.json',{'phase':'plan_review'})
                finish_attempt(job,ticket,'reviewing',candidate=candidate,candidate_validated=True)
                phase='plan_review';candidate_review=engine.review_plan(job,candidate,sources,request)
                pv.save_json(folder/'plan_review.json',candidate_review)
                if not _accepted(candidate_review):
                    unavailable=not _review_available(candidate_review)
                    finish_attempt(job,ticket,'review_unavailable' if unavailable else 'rejected',
                        candidate=candidate,review=candidate_review,candidate_validated=True)
                    working=candidate;feedback=candidate_review
                    if not unavailable:rejected_reviews[engine.plan_hash(candidate)]=candidate_review
                    last_reason='修订计划审核未完成，保留候选待重审。' if unavailable else '修订计划仍有待处理问题。'
                    if unavailable:stop=True
                    if stop:break
                    continue
                phase='render'
                pv.save_json(analysis/'studio_plan.json',candidate)
                engine.export(job,candidate,sources,request)
                candidate=render_with_recovery(job,request,candidate,sources)
                # Timing repairs may consume the remaining shared attempts and
                # change shots. Bind final review to the exact resulting plan.
                engine.validate(candidate,sources,request.get('script','').strip())
                round_number+=1
                saved={'phase':'final_review','round':round_number,
                       'hashes':{name:file_hash(job/name) for name in ('project.json','output/video.mp4','analysis/studio_plan.json')}}
                pv.save_json(folder/'candidate.json',candidate)
                pv.save_json(folder/'resume.json',saved)
                candidate_checkpoint=capture_checkpoint(job,folder/'rendered')
                pv.save_json(folder/'rendered_checkpoint.json',candidate_checkpoint)
            else:
                round_number=int(saved['round'])
            phase='final_review'
            finish_attempt(job,ticket,'reviewing',candidate=candidate,candidate_validated=True)
            measured,_=final_review(job,request,candidate,sources,engine.call_ai,engine.REVIEW_SCHEMA,round_number)
            pv.save_json(folder/'final_review.json',measured)
            if _accepted(measured):
                finish_attempt(job,ticket,'accepted',candidate=candidate,review=measured,candidate_validated=True)
                accepted=True
                return candidate,measured
            unavailable=not _review_available(measured)
            finish_attempt(job,ticket,'review_unavailable' if unavailable else 'rejected',
                candidate=candidate,review=measured,candidate_validated=True)
            working=candidate;feedback=measured
            if not unavailable:rejected_reviews[engine.plan_hash(candidate)]=measured
            last_reason='返修成片审核未完成，保留同一份候选视频待重审。' if unavailable else '返修成片仍有待处理问题。'
            if unavailable:break
        except Exception as exc:
            import os
            detail=redact(f'{type(exc).__name__}: {exc}',[request.get('key',''),request.get('api_key',''),
                os.environ.get('PAPER_VIDEO_API_KEY',''),os.environ.get('OPENAI_API_KEY','')])[:1500]
            status=('review_unavailable' if phase in ('plan_review','final_review') and validated else
                    'invalid_candidate' if phase=='validation' else 'generation_failed' if phase=='generation' else 'render_failed')
            finish_attempt(job,ticket,status,candidate=candidate,review=candidate_review,
                           reason=detail,candidate_validated=validated)
            last_reason=detail
            pv.log('  返修未完成，保留上一份成片：'+detail[:250])
            from studio_repair_budget import generation_retryable
            if phase=='validation' or (phase=='generation' and generation_retryable(exc)):
                if candidate is not None:working=candidate
                feedback={'ready':False,'issues':[],'validation_error':detail}
            else:break  # Service/render failures need recovery, not blind regeneration.
        finally:
            if not accepted:restore_checkpoint(checkpoint)
    result=dict(original_final)
    result['repair_error']=last_reason or '自动返修额度已用完，保留现有成片和具体审核意见。'
    pv.save_json(job/'output/final_review.json',result)
    return original_plan,result
