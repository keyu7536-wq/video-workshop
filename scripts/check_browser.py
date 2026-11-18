"""Run an isolated Chromium smoke check against a running local workbench.

The browser reads application configuration and status. Generation and plan
writes are intercepted by the test fixture; no AI or TTS request is made.
"""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse
import uuid

ROOT = Path(__file__).resolve().parents[1]


def browser_path(explicit: str | None) -> str:
    candidates = [explicit] if explicit else [
        shutil.which('chromium'), shutil.which('chromium-browser'),
        shutil.which('google-chrome'), shutil.which('msedge'),
        'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
        'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise SystemExit('A Chromium-based browser is required; pass --browser /path/to/browser.')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Running local server URL, e.g. http://127.0.0.1:8780')
    parser.add_argument('--browser', help='Path to Edge, Chrome or Chromium')
    parser.add_argument('--output', type=Path, help='Optional JSON result file')
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or parsed.path not in ('', '/'):
        raise SystemExit('--url must name a local HTTP server root.')
    browser = browser_path(args.browser)
    page = ROOT / 'studio' / f'qa-{uuid.uuid4().hex}.html'
    page.write_bytes((ROOT / 'tests' / 'portable_ui.html').read_bytes())
    try:
        with tempfile.TemporaryDirectory(prefix='video-workshop-ui-') as profile:
            result = subprocess.run([
                browser, '--headless=new', '--disable-gpu', '--no-first-run',
                '--user-data-dir=' + profile, '--dump-dom', '--virtual-time-budget=15000',
                args.url.rstrip('/') + '/' + page.name,
            ], capture_output=True, timeout=45,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            dom = result.stdout.decode('utf-8', errors='replace')
            match = re.search(r'<pre id="result">(.*?)</pre>', dom, re.S)
            if not match:
                raise RuntimeError('Browser did not return the test result; check server URL and browser startup.')
            try:
                report = json.loads(html.unescape(match.group(1)))
            except json.JSONDecodeError as exc:
                raise RuntimeError('Browser checks did not finish within the allotted virtual time.') from exc
            raw = json.dumps(report, ensure_ascii=False, indent=2)
            if args.output:
                args.output.write_text(raw, encoding='utf-8')
            print(raw)
            if not report.get('passed'):
                raise SystemExit(1)
    finally:
        page.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
