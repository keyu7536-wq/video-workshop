"""Bounded program-side recovery for measured visual-duration shortages."""
from pathlib import Path
import json

import paper_video as pv


def render_with_recovery(job,request,plan,sources):
    import studio_engine as engine
    from studio_revision import file_hash
    diagnostic_path=job/'output/timing_recovery.json'
    if diagnostic_path.is_file():
        # Retain the previous diagnosis, but never repair from a stale attempt.
        (job/'analysis/timing_recovery_previous.json').write_bytes(diagnostic_path.read_bytes())
        diagnostic_path.unlink()
    try:
        engine.render(job)
        return plan
    except RuntimeError as render_error:
        try:diagnostic=pv.load_json(diagnostic_path)
        except (OSError,ValueError):raise render_error
        if (not request.get('auto_repair',True)
                or diagnostic.get('status')!='needs_more_visuals'
                or diagnostic.get('code')!='insufficient_visual_duration'
                or diagnostic.get('project_sha256')!=file_hash(job/'project.json')
                or not diagnostic.get('issues')):
            raise
    pv.log('【成片与导出】画面长度不足，按实际配音自动补充相关镜头（最多一次）')
    folder=job/'analysis/timing_recovery';folder.mkdir(exist_ok=True)
    pv.save_json(folder/'diagnostic.json',diagnostic)
    pv.save_json(folder/'plan_before.json',plan)
    old_review=(job/'review.json').read_bytes() if (job/'review.json').is_file() else None
    if old_review is not None:(folder/'original_review.json').write_bytes(old_review)
    images=pv.load_json(job/'analysis/images.json') if (job/'analysis/images.json').is_file() else []
    prompt=engine.prompt_for(sources,images,request)
    prompt+='\n本次只修正镜头长度不足，不能改写、删减、拆分或重排任何段落的id和narration。保留已经正确的镜头与事实。'
    prompt+='诊断blocks的cue_candidates提供原文中的唯一短语与真实词边界start，cue_time_basis=block_local表示本段起点后的秒数，不是全片秒数。列表最多48条、每条最多24字，长段只是有界采样，不表示所有可用切点，也不表示每一处在语义上都适合切镜。不要按字数或duration权重重新估算已有配音时间。'
    prompt+='遇到complete_clip_exceeds_cue_window，优先在语义允许的更早实测cue切入同一动作视频，保持其source_id、in、duration、keep_full和后续关键演示切点。用后一个实际cue时间减去候选start检查完整取段能否放下，并核对前一镜头及本段其他完整动作仍有足够时间。修改cue只改变计划切点，不会重写或挪动原始配音；不能指望渲染器暗中提前cue。'
    prompt+='旁白若要求观看奔跑、抓取、开合等实际运动或动作变化，必须保留对应运动画面，不能换成同主体静图、论文截图或概念示意冒充动作已经展示。这个判断依据完整旁白和素材实际内容，不是见到某个动词就机械判断。解释机制或背景的余量才可用现有相关图、论文图或准确原理示意承接；不要慢放、循环演示、长时间冻结原片，不能生成大段文字卡凑时长。'
    prompt+='keep_full是必须保留完整取段的要求：本次时序修复必须在原段保留每一处原source_id、原in、不少于原duration且keep_full=true的镜头，不能整段换成静图/其他视频，不能移到别段，也不能用同一个候选镜头抵掉多个原取段。允许改变其cue或补充其他镜头。需要换素材由用户明确修改或独立内容修订阶段决定。若没有既语义对应又容得下完整动作的切点，保留原镜头和明确问题，让此次修复失败；不能改旁白、删除正在讲述的动作或冒充已解决。'
    prompt+='原片不够且没有可信解释画面时保留明确问题，不得编造实验或改写旁白掩盖缺口。'
    prompt+='\n当前完整计划：'+json.dumps(plan,ensure_ascii=False)+'\n实测缺口：'+json.dumps(diagnostic,ensure_ascii=False)
    fixed=engine.call_ai(prompt,[Path(path) for _,path in images],job/'analysis',request,engine.SCHEMA,'_timing_repair')
    if [(b['id'],b['narration']) for b in fixed.get('blocks',[])]!=[(b['id'],b['narration']) for b in plan['blocks']]:
        raise ValueError('自动补镜头修改了原文稿，已保留原计划。请在分镜页面补充相关画面后重试。')
    for before,after in zip(plan['blocks'],fixed['blocks']):
        remaining=[shot for shot in after['shots'] if shot['kind']=='video']
        for original in before['shots']:
            if original['kind']!='video' or not original.get('keep_full'):continue
            match=next((index for index,shot in enumerate(remaining)
                        if shot['source_id']==original['source_id']
                        and abs(shot['in']-original['in'])<.01
                        and shot.get('keep_full') is True
                        and shot['duration']>=original['duration']-.01),None)
            if match is None:
                raise ValueError('自动补镜头移除或截短了要求完整保留的动作，已保留原计划；只能调整切点或增加解释镜头。')
            remaining.pop(match)
    engine.validate(fixed,sources,request.get('script','').strip())
    engine.validate_visual_policy(fixed,request)
    pv.save_json(folder/'candidate.json',fixed)
    feedback=None
    try:
        review=engine.review_plan(job,fixed,sources,request)
        if not review['ready'] or any(i['severity']=='error' for i in review['issues']):
            pv.save_json(folder/'rejected_review.json',review)
            from studio_plan_repair import unchanged_block_feedback
            feedback=unchanged_block_feedback(job,plan,fixed,review,
                json.loads(old_review) if old_review is not None else None,sources,folder)
            raise ValueError('自动补充的镜头未通过内容审核，已保留原文稿与分镜，请查看任务中的审核原因。')
    except Exception:
        if feedback is not None:pv.save_json(job/'review.json',feedback)
        elif old_review is not None:(job/'review.json').write_bytes(old_review)
        elif (job/'review.json').exists():(job/'review.json').unlink()
        raise
    pv.save_json(job/'analysis/studio_plan.json',fixed)
    engine.export(job,fixed,sources,request)
    pv.save_json(folder/'plan_after.json',fixed)
    # This second attempt is deliberately not recursive.
    engine.render(job)
    return fixed
