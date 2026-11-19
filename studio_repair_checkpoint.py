"""Restore generated artifacts after an unsuccessful repair, retaining evidence."""
from pathlib import Path
import re
import shutil


_ROOT_FILES=('project.json','narration.txt','evidence.json','publishing.json',
             'review.json','analysis/studio_plan.json')


def _within(path,root):
    path=Path(path).resolve()
    if path==root or not path.is_relative_to(root):
        raise ValueError('返修备份路径必须位于当前任务目录内。')
    return path


def _generated_files(job):
    paths=[job/name for name in _ROOT_FILES if (job/name).is_file()]
    paths.extend(path for path in (job/'output').glob('*')
                 if path.is_file() and path.name!='timing_recovery.json')
    paths.extend(path for path in (job/'assets').glob('*')
                 if path.is_file() and re.fullmatch(r'(?:visual_\d+_\d+\.png|diagram_\d+_\d+\.mp4)',path.name))
    paths.extend(path for path in (job/'analysis/final_frames').rglob('*') if path.is_file())
    for path in paths:_within(path,job)
    return paths


def capture_checkpoint(job,folder):
    """Copy only generated artifacts; never link files that a renderer overwrites."""
    job=Path(job).resolve();folder=_within(folder,job)
    if not folder.is_relative_to(job/'analysis') or folder.is_relative_to(job/'analysis/final_frames'):
        raise ValueError('返修备份必须保存在当前任务的 analysis 目录。')
    folder.mkdir(parents=True,exist_ok=True)
    files=[path.relative_to(job).as_posix() for path in _generated_files(job)]
    for name in files:
        target=_within(folder/name,folder);target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(job/name,target)
    return {'job':str(job),'folder':str(folder),'files':files}


def restore_checkpoint(checkpoint):
    """Restore this job's prior outputs, leaving diagnostics/candidates untouched."""
    job=Path(checkpoint['job']).resolve();folder=_within(checkpoint['folder'],job)
    files=set(checkpoint['files'])
    # Validate the complete restore set before making any filesystem mutation.
    targets={name:_within(job/name,job) for name in files}
    backups={name:_within(folder/name,folder) for name in files}
    for backup in backups.values():
        if not backup.is_file():raise ValueError('返修备份不完整，未覆盖当前任务。')
    current=_generated_files(job)
    for path in current:
        if path.relative_to(job).as_posix() not in files:path.unlink()
    for name,target in targets.items():
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(backups[name],target)
