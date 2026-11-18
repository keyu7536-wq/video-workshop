"""Create a local environment on first use; then start the loopback UI."""
from pathlib import Path
import hashlib
import subprocess
import sys
import venv
import os

def main():
    if sys.version_info < (3,11):
        raise RuntimeError('Python 3.11 or newer is required; Python 3.12 is recommended.')
    root=Path(__file__).resolve().parents[1]
    env=root/'.venv'
    python=env/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    if not python.is_file():venv.EnvBuilder(with_pip=True).create(env)
    signature=hashlib.sha256(b''.join((root/name).read_bytes() for name in ('requirements.txt','requirements-paper.txt'))).hexdigest()
    stamp=env/'.video-workshop-dependencies'
    if not stamp.exists() or stamp.read_text()!=signature:
        subprocess.run([str(python),'-m','pip','install','-r',str(root/'requirements-paper.txt')],cwd=root,check=True)
        stamp.write_text(signature)
    return subprocess.call([str(python),str(root/'studio_server.py'),*sys.argv[1:]],cwd=root)

if __name__=='__main__':
    try:sys.exit(main())
    except (RuntimeError,OSError,subprocess.CalledProcessError) as error:
        print(str(error),file=sys.stderr);sys.exit(1)
