"""Run background Python workers with explicit streams and useful failures."""
from collections import deque
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


_job_operations = threading.local()


@contextmanager
def job_operation(job, *, wait=False):
    """Protect a short job mutation across threads and studio processes.

    The OS releases this lock if the process exits. The persistent lock file is
    not a running-worker marker; job.json/worker.json remain the run records.
    A Handler can safely call Manager.start while holding the same transaction.
    """
    job = Path(job).resolve()
    key = os.path.normcase(str(job))
    held = getattr(_job_operations, 'held', None)
    if held is None:
        held = _job_operations.held = set()
    if key in held:
        yield
        return
    lock_path = job / 'analysis/operation.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'\0'); stream.flush()
        stream.seek(0)
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if not wait:
                    raise ValueError('此任务正由另一个请求更新，请稍后重试。') from exc
                # Collectors must finish recording a worker's result even when
                # a short external HTTP transaction currently holds the lock.
                time.sleep(.05)
        held.add(key)
        try:
            yield
        finally:
            held.remove(key)
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def process_alive(pid):
    """Return True/False only when the OS confirms it; None means unknown."""
    if type(pid) is not int or not 0 < pid <= 0xffffffff:
        return None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False if ctypes.get_last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError, OverflowError):
        return None
    return True


def console_python(executable=None):
    """Stay in the same environment, but avoid pythonw's absent child streams."""
    executable = Path(executable or sys.executable)
    if executable.name.lower() == 'pythonw.exe':
        candidate = executable.with_name('python.exe')
        if not candidate.is_file():
            raise RuntimeError('当前 Python 环境缺少 python.exe，无法启动制作进程。')
        return str(candidate)
    return str(executable)


def redact(text, secrets=()):
    for secret in secrets:
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return text


def failure_detail(label, returncode, lines, log_path):
    tail = '\n'.join(line.rstrip() for line in lines if line.strip())[-1400:]
    message = f'{label}失败（退出码 {returncode}）。日志：{log_path}'
    return message + ('\n' + tail if tail else '\n子进程没有返回错误详情。')


def run_logged(command, log_path, *, cwd=None, env=None, on_line=None, label='视频合成'):
    """Stream both channels to disk and the GUI; keep stderr in raised errors."""
    environment = {**(os.environ if env is None else env), 'PYTHONUTF8':'1', 'PYTHONIOENCODING':'utf-8'}
    secrets = [environment.get(name, '') for name in ('PAPER_VIDEO_API_KEY', 'OPENAI_API_KEY')]
    log_path = Path(log_path); log_path.parent.mkdir(parents=True, exist_ok=True)
    tail = deque(maxlen=60)
    with log_path.open('w', encoding='utf-8') as log:
        try:
            process = subprocess.Popen(command, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding='utf-8', errors='replace', bufsize=1,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as exc:
            message = redact(f'{label}未能启动：{exc}', secrets)
            log.write(message+'\n')
            raise RuntimeError(message) from exc
        with process:
            for raw in process.stdout:
                line = redact(raw, secrets)
                log.write(line); log.flush(); tail.append(line)
                if on_line: on_line(line.rstrip('\r\n'))
            returncode = process.wait()
        if returncode:
            raise RuntimeError(failure_detail(label, returncode, tail, log_path))
    return returncode
