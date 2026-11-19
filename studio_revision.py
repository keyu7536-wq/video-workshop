"""Bind reviews to the settings and bytes actually reviewed."""
import hashlib
import json
import paper_video as pv
from studio_display import DISPLAY_KEYS

def render_settings_hash(request):
    from studio_story import normalize_framework,STORY_POLICY_VERSION
    keys=('voice','rate','voice_style','delivery_scope','content_type',*DISPLAY_KEYS)
    settings={k:request.get(k) for k in keys}
    settings.update(story_framework=normalize_framework(request.get('story_framework')),story_policy_version=STORY_POLICY_VERSION)
    if request.get('_brief_revision') is not None:settings['_brief_revision']=request['_brief_revision']
    from studio_duration import effective_duration_settings
    duration=effective_duration_settings(request)
    if duration['duration_mode']=='adaptive' or request.get('_duration_revision') is not None:
        settings['duration_settings']=duration
    return hashlib.sha256(json.dumps(settings,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def file_hash(path):
    # Metadata is not a content identity: same-size writes can retain/coarsen
    # mtime (notably on Windows). Review/export guards must hash current bytes.
    digest=hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()

def review_stale(job,review,plan_hash):
    video=job/'output/video.mp4';project=job/'project.json'
    return (review.get('plan_hash')!=plan_hash
        or review.get('settings_hash')!=render_settings_hash(pv.load_json(job/'request.json'))
        or not video.is_file() or review.get('video_sha256')!=file_hash(video)
        or not project.is_file() or review.get('project_sha256')!=file_hash(project))


def export_version_current(job):
    """Reject stale known renders; retain the status-gated legacy export path."""
    from studio_engine import plan_hash
    proof=job/'output/render_receipt.json'
    if not proof.is_file():proof=job/'output/final_review.json'
    if not proof.is_file():return True
    try:
        plan=pv.load_json(job/'analysis/studio_plan.json')
        return not review_stale(job,pv.load_json(proof),plan_hash(plan))
    except (OSError,ValueError,KeyError,TypeError):return False
