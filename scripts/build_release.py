"""Build an allowlisted, inspectable source release without runtime/user data.

Run with --check first. Existing releases are never overwritten.
This scanner is a release guard, not a comprehensive secret detector.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SLUG = 'video-workshop'
REQUIRED = (
    'README.md', 'CHANGELOG.md', '.gitignore', '.gitattributes', '.editorconfig', 'start.cmd', 'start.sh', 'app_config.py', 'app.example.json',
    'LICENSE', 'THIRD_PARTY_NOTICES.md', 'CONTRIBUTING.md', 'SECURITY.md',
    'docs/production-workflow.md', 'docs/project-audit.md', 'docs/configuration.md', 'docs/story-and-visuals.md',
    'docs/media-sources.md', 'docs/testing.md',
    '.github/ISSUE_TEMPLATE/bug_report.yml', '.github/ISSUE_TEMPLATE/feature_request.yml',
    '.github/ISSUE_TEMPLATE/config.yml', '.github/PULL_REQUEST_TEMPLATE.md',
    'requirements.txt', 'requirements-paper.txt', 'requirements-dev.txt',
    'requirements-voice.txt', 'make_video.py', 'paper_video.py', 'model_api.py',
    'local_voice_worker.py', 'studio_crop.py', 'studio_delivery.py', 'studio_diagrams.py',
    'studio_display.py', 'studio_engine.py', 'studio_local_voice.py', 'studio_console.py', 'studio_duration.py',
    'studio_render.py', 'studio_research.py', 'studio_revision.py',
    'studio_server.py', 'studio_voice.py', 'studio_story.py', 'studio_documents.py', 'studio_context.py', 'studio_process.py', 'studio_recovery.py',
    'studio_network.py', 'studio_search.py', 'studio_plan_repair.py', 'studio_repository.py', 'studio_review_assets.py', 'studio_media.py',
    'studio/index.html', 'studio/app.js', 'studio/display.js', 'studio/preview.js', 'studio/style.css', 'studio/voice-audition.html',
    'skills/paper-video-director/SKILL.md',
    'skills/paper-video-director/agents/openai.yaml',
    'skills/paper-video-director/references/production.md',
    'skills/paper-video-director/references/review.md',
    'scripts/build_release.py', 'scripts/smoke_render.py', 'scripts/check_browser.py', 'scripts/bootstrap.py',
    'scripts/download_voice_models.py', 'scripts/install_voice.ps1',
    'scripts/voice-constraints.txt',
    'scripts/voice-manifests/qwen-aligner.json',
    'scripts/voice-manifests/qwen-custom.json',
    'scripts/voice-manifests/whisper-small.json',
    'tests/test_release_package.py', 'tests/portable_ui.html', 'tests/test_card_layout.py',
    'tests/test_display_settings.py', 'tests/test_diagrams.py', 'tests/test_diagram_pipeline.py', 'tests/test_local_voice.py',
    'tests/test_model_api.py', 'tests/test_paper_video.py', 'tests/test_studio.py',
    'tests/test_studio_refinements.py', 'tests/test_studio_workflow.py', 'tests/test_portable_app.py',
    'tests/test_story_framework.py', 'tests/test_story_integration.py',
    'tests/test_source_intake.py', 'tests/test_research_documents.py', 'tests/test_documents.py', 'tests/test_context.py', 'tests/test_dossier_batches.py', 'tests/test_ui_errors.py',
    'tests/test_studio_process.py', 'tests/test_timing_recovery.py', 'tests/test_render_recovery.py', 'tests/test_review_availability.py', 'tests/test_review_frames.py', 'tests/test_job_recovery.py',
    'tests/test_paper_triage.py', 'tests/test_network_retry.py', 'tests/test_public_search.py', 'tests/test_plan_repair.py', 'tests/test_shot_preview.py',
    'tests/test_project_refresh.py',
    'tests/test_visual_policy_review.py', 'tests/test_repository_fallback.py', 'tests/test_review_feedback.py', 'tests/test_review_assets.py', 'tests/test_plan_review_context.py',
    'tests/test_author_media.py', 'tests/test_audio_playback_ui.py', 'tests/test_backend_isolation.py', 'tests/test_console_output.py',
    'tests/test_plan_gate.py', 'tests/test_effective_canvas.py', 'tests/test_duration.py', 'tests/test_model_response_safety.py', 'tests/test_atomic_json.py',
)
OPTIONAL = (
    'pyproject.toml', 'NOTICE', 'NOTICE.md',
    '.github/workflows/tests.yml', '.github/workflows/ci.yml',
)
# These expressions intentionally match high-confidence values, not variable names.
# Diagnostics report only path/line/category, never the matched value.
GUARDS = (
    ('private-local-path', re.compile(r'(?:[A-Za-z]:[/\\](?:Users[/\\][^/\\\s\"\']+|code[/\\])|/(?:home|Users)/[^/\s\"\']+/)', re.I)),
    ('private-job-id', re.compile(r'\b20\d{6}_\d{6}_(?:studio_)?[0-9a-f]{6,}\b')),
    ('api-token', re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b')),
    ('github-token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b')),
    ('private-key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
)

def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def selected_files(root: Path) -> list[Path]:
    root = root.resolve()
    missing = [name for name in REQUIRED if not (root/name).is_file()]
    if missing:
        raise ValueError('Required release files missing: ' + ', '.join(missing))
    files = [root/name for name in (*REQUIRED, *OPTIONAL) if (root/name).is_file()]
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Release input must be a local regular file: ' + path.relative_to(root).as_posix())
        # Reject symlink/junction parent directories, including in-tree links.
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
                raise ValueError('Linked release directory: ' + path.relative_to(root).as_posix())
    return sorted(set(files))

def inspect(root: Path, files: list[Path]) -> list[dict]:
    findings = []
    for path in files:
        try:
            lines = path.read_text(encoding='utf-8-sig').splitlines()
        except UnicodeDecodeError:
            findings.append({'path':path.relative_to(root).as_posix(), 'line':0, 'category':'non-text-source'})
            continue
        for number,line in enumerate(lines,1):
            for category,pattern in GUARDS:
                if pattern.search(line):
                    findings.append({'path':path.relative_to(root).as_posix(), 'line':number, 'category':category})
    return findings

def build(root: Path, destination: Path, version: str) -> dict:
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[a-z0-9.-]+)?',version):
        raise ValueError('Version must be a semantic version such as 0.1.0')
    root = root.resolve(); destination = destination.resolve()
    files = selected_files(root); findings = inspect(root,files)
    if findings:
        raise ValueError('Release scan blocked: ' + json.dumps(findings,ensure_ascii=False))
    target = destination/f'{SLUG}-{version}'
    archive = destination/f'{SLUG}-{version}.zip'
    checksum = destination/f'{SLUG}-{version}.zip.sha256'
    if any(path.exists() for path in (target,archive,checksum)):
        raise ValueError('Release already exists; choose a new version or empty destination')
    destination.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.release-',dir=destination) as temporary:
        staging = Path(temporary)/SLUG; staging.mkdir()
        for path in files:
            output = staging/path.relative_to(root);output.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,output)
        # Scan the snapshot as well, so a concurrent source edit cannot bypass inspection.
        snapshot_files = [staging/path.relative_to(root) for path in files]
        findings = inspect(staging,snapshot_files)
        if findings:
            raise ValueError('Snapshot scan blocked: ' + json.dumps(findings,ensure_ascii=False))
        manifest = {'project':SLUG,'version':version,'kind':'source-release',
            'includes_runtime_data':False,'secret_scan':'High-confidence text patterns only; manual review remains necessary',
            'files':[{'path':p.relative_to(staging).as_posix(),'size':p.stat().st_size,'sha256':sha256(p)} for p in snapshot_files]}
        (staging/'RELEASE_MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        temporary_zip = Path(temporary)/'release.zip'
        with zipfile.ZipFile(temporary_zip,'w',zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(staging.rglob('*')):
                if not path.is_file():continue
                relative=path.relative_to(staging).as_posix()
                info=zipfile.ZipInfo(f'{SLUG}/{relative}',date_time=(2020,1,1,0,0,0))
                info.compress_type=zipfile.ZIP_DEFLATED
                info.external_attr=((0o100755 if relative.endswith('.sh') else 0o100644)<<16)
                bundle.writestr(info,path.read_bytes())
        with zipfile.ZipFile(temporary_zip) as bundle:
            if bundle.testzip() is not None:
                raise ValueError('Release archive integrity check failed')
        digest=sha256(temporary_zip)
        staging.rename(target);temporary_zip.rename(archive)
        checksum.write_text(f'{digest}  {archive.name}\n',encoding='utf-8')
    return {'folder':str(target),'archive':str(archive),'sha256':digest,'files':len(files)}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--output',type=Path,default=ROOT/'dist')
    parser.add_argument('--version',default='0.3.2')
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args();root=args.root.resolve()
    if args.check:
        files=selected_files(root);findings=inspect(root,files)
        print(json.dumps({'files':len(files),'passed':not findings,'findings':findings},ensure_ascii=True,indent=2))
        raise SystemExit(1 if findings else 0)
    print(json.dumps(build(root,args.output,args.version),ensure_ascii=True,indent=2))

if __name__=='__main__':main()
