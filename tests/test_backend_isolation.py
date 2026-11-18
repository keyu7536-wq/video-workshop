"""Exercise HTTP mutation isolation with disposable jobs and real short workers."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import paper_video as pv
import studio_server as server


class BackendIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.job = self.root / 'jobs/fixture'
        (self.job / 'analysis').mkdir(parents=True)
        pv.save_json(self.job / 'job.json', {'status': 'prepared', 'engine': 'studio'})
        pv.save_json(self.job / 'request.json', {'backend': 'codex', 'model': 'original'})
        pv.save_json(self.job / 'analysis/studio_plan.json', {'title': 'fixture', 'blocks': []})
        (self.root / 'studio_engine.py').write_text(
            'import json,sys,time\nfrom pathlib import Path\n'
            'path=Path(sys.argv[2])/"job.json"\ntime.sleep(1)\n'
            'state=json.loads(path.read_text())\nstate["status"]="completed"\n'
            'path.write_text(json.dumps(state))\n', encoding='utf-8')
        root_patch = patch.object(server, 'ROOT', self.root)
        root_patch.start(); self.addCleanup(root_patch.stop)
        self.http = self.new_server()

    def new_server(self):
        http = server.StudioHTTPServer(('127.0.0.1', 0), server.Handler)
        http.manager = server.Manager()
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        self.addCleanup(self.wait_worker, http.manager)
        return http

    def wait_worker(self, manager):
        if manager.worker:
            manager.worker.join(10)
            self.assertFalse(manager.worker.is_alive(), 'Disposable fixture worker did not finish')

    def post(self, http, data, endpoint='/api/render'):
        request = Request(f'http://127.0.0.1:{http.server_port}' + endpoint,
            data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            return response.code, json.load(response)

    def overlapping_requests(self, second_server=None):
        entered = threading.Event(); release = threading.Event()
        original = server.model_settings
        original_start = server.Manager.start
        outcomes = {}
        started = []
        original_plan = (self.job / 'analysis/studio_plan.json').read_bytes()

        def observed_start(manager, *args, **kwargs):
            result = original_start(manager, *args, **kwargs)
            started.append((manager.process.pid, (self.job / 'job.json').read_bytes()))
            return result

        def slow_settings(request, data):
            if data.get('model') == 'first':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('Fixture barrier expired')
            return original(request, data)

        def send(name):
            http = second_server if name == 'second' and second_server else self.http
            outcomes[name] = self.post(http, {'job_id': self.job.name, 'model': name})

        with patch.object(server, 'model_settings', side_effect=slow_settings), \
                patch.object(server.Manager, 'start', new=observed_start), \
                patch.object(server.subprocess, 'Popen', wraps=subprocess.Popen) as launch:
            first = threading.Thread(target=send, args=('first',))
            second = threading.Thread(target=send, args=('second',))
            first.start()
            try:
                self.assertTrue(entered.wait(5), 'First request did not reach settings')
                second.start()
                # Old code accepts second while first is still preparing settings.
                second.join(.4)
            finally:
                release.set(); first.join(10)
                if second.ident is not None: second.join(10)
            self.assertEqual(launch.call_count, 1, 'Only one worker may start')
        self.assertEqual(sorted(result[0] for result in outcomes.values()), [200, 400], outcomes)
        accepted = next(name for name, result in outcomes.items() if result[0] == 200)
        self.assertEqual(pv.load_json(self.job / 'request.json')['model'], accepted,
            f'A rejected request changed the accepted settings: {outcomes}')
        self.assertEqual(len(started), 1)
        self.assertEqual((self.job / 'job.json').read_bytes(), started[0][1])
        self.assertEqual((self.job / 'analysis/studio_plan.json').read_bytes(), original_plan)

    def test_rejected_overlapping_request_cannot_replace_accepted_settings(self):
        self.overlapping_requests()

    def test_two_managers_cannot_mutate_the_same_job_before_start(self):
        self.overlapping_requests(self.new_server())

    def test_external_process_lock_covers_every_mutation_and_direct_start(self):
        """Use a real OS lock owner, not a mock or a shared Python thread lock."""
        script = ('from pathlib import Path\nimport sys\n'
            'from studio_process import job_operation\n'
            'with job_operation(Path(sys.argv[1])):\n'
            ' print("locked",flush=True)\n'
            ' sys.stdin.read()\n')
        process = subprocess.Popen([sys.executable, '-u', '-c', script, str(self.job)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', cwd=Path(server.__file__).resolve().parent,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            self.assertEqual(process.stdout.readline().strip(), 'locked')
            files = ('job.json', 'request.json', 'analysis/studio_plan.json')
            before = {name: (self.job / name).read_bytes() for name in files}
            for endpoint in ('/api/plan', '/api/resume', '/api/render', '/api/review', '/api/export', '/api/replan'):
                with self.subTest(endpoint=endpoint):
                    code, body = self.post(self.http,
                        {'job_id': self.job.name, 'model': 'must-not-save', 'plan': {}}, endpoint)
                    self.assertEqual(code, 400, body)
                    self.assertIn('另一个请求', body['error'])
                    self.assertEqual({name: (self.job / name).read_bytes() for name in files}, before)
            with patch.object(server.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(ValueError, '另一个请求'):
                    server.Manager().start(self.job, 'render')
                launch.assert_not_called()
            self.assertEqual({name: (self.job / name).read_bytes() for name in files}, before)
        finally:
            stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        # The existing lock file is harmless after its OS owner releases it.
        code, result = self.post(self.http, {'job_id': self.job.name, 'model': 'after-release'})
        self.assertEqual(code, 200, result)
        self.assertEqual(pv.load_json(self.job / 'request.json')['model'], 'after-release')

    def test_os_releases_job_lock_after_abrupt_owner_exit(self):
        """A crashed studio cannot strand a marker that blocks future edits."""
        script = ('from pathlib import Path\nimport os,sys\n'
            'from studio_process import job_operation\n'
            'with job_operation(Path(sys.argv[1])):\n'
            ' print("locked",flush=True)\n'
            ' sys.stdin.read()\n'
            ' os._exit(7)\n')
        process = subprocess.Popen([sys.executable, '-u', '-c', script, str(self.job)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', cwd=Path(server.__file__).resolve().parent,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            self.assertEqual(process.stdout.readline().strip(), 'locked')
            code, _ = self.post(self.http, {'job_id': self.job.name, 'model': 'blocked'})
            self.assertEqual(code, 400)
        finally:
            process.communicate(timeout=10)
        self.assertEqual(process.returncode, 7)
        code, result = self.post(self.http, {'job_id': self.job.name, 'model': 'after-crash'})
        self.assertEqual(code, 200, result)

    def test_active_manager_rejects_create_without_leaving_an_orphan_job(self):
        self.http.manager.start(self.job, 'render')
        with patch.object(server.engine, 'new_job') as create:
            code, result = self.post(self.http, {'script': 'A new draft'}, '/api/create')
        self.assertEqual(code, 400, result)
        create.assert_not_called()

    def test_lock_released_after_invalid_request(self):
        code, _ = self.post(self.http, {'job_id': self.job.name, 'backend': 'invalid'})
        self.assertEqual(code, 400)
        self.assertEqual(pv.load_json(self.job / 'request.json')['model'], 'original')
        code, result = self.post(self.http, {'job_id': self.job.name, 'model': 'valid'})
        self.assertEqual(code, 200, result)

    def exited_pid(self):
        process = subprocess.Popen([sys.executable, '-c', 'pass'],
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(process.wait(timeout=10), 0)
        return process.pid

    def interrupted_job(self, worker_pid, server_pid, recorded_run='fixture-run'):
        pv.save_json(self.job / 'job.json', {'engine': 'studio', 'status': 'processing',
            'run_id': 'fixture-run', 'stage': 'render', 'last_action': 'render',
            'output': 'keep-existing-video.mp4'})
        pv.save_json(self.job / 'analysis/worker.json', {'run_id': recorded_run,
            'worker_pid': worker_pid, 'server_pid': server_pid, 'action': 'render'})

    def test_interrupted_job_recovers_only_after_recorded_owner_and_worker_exit(self):
        self.interrupted_job(self.exited_pid(), self.exited_pid())
        result = server.snapshot(self.job)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['recovery']['action'], 'render')
        state = pv.load_json(self.job / 'job.json')
        self.assertEqual(state['output'], 'keep-existing-video.mp4')
        self.assertEqual(state['run_id'], 'fixture-run')
        self.assertEqual(state['failed_action'], 'render')
        self.assertIn('退出', state['error'])
        code, body = self.post(self.http, {'job_id': self.job.name})
        self.assertEqual(code, 200, body)

    def test_interrupted_job_does_not_override_live_process_or_mismatched_run(self):
        dead = self.exited_pid()
        for worker, owner, run in ((dead, os.getpid(), 'fixture-run'),
                (os.getpid(), dead, 'fixture-run'), (dead, dead, 'other-run')):
            with self.subTest(worker=worker, owner=owner, run=run):
                self.interrupted_job(worker, owner, run)
                before = (self.job / 'job.json').read_bytes()
                self.assertEqual(server.snapshot(self.job)['status'], 'processing')
                self.assertEqual((self.job / 'job.json').read_bytes(), before)
                code, _ = self.post(self.http, {'job_id': self.job.name})
                self.assertEqual(code, 400)
                self.assertEqual((self.job / 'job.json').read_bytes(), before)

    def test_unknown_process_liveness_keeps_processing_state(self):
        self.interrupted_job(100, 101)
        before = (self.job / 'job.json').read_bytes()
        with patch('studio_process.process_alive', return_value=None):
            self.assertEqual(server.snapshot(self.job)['status'], 'processing')
        self.assertEqual((self.job / 'job.json').read_bytes(), before)

    def test_collector_cannot_overwrite_a_new_run_between_read_and_save(self):
        pv.save_json(self.job / 'job.json', {'status': 'completed', 'run_id': 'old-run'})
        original_load = pv.load_json
        read = threading.Event(); release = threading.Event()

        def slow_old_read(path):
            state = original_load(path)
            if Path(path) == self.job / 'job.json' and threading.current_thread().name == 'old-collector':
                read.set()
                if not release.wait(5): raise RuntimeError('Collector fixture barrier expired')
            return state

        old_manager = server.Manager()
        process = SimpleNamespace(stdout=io.StringIO(''), wait=lambda: 0)
        log = (self.job / 'analysis/old-fixture.log').open('w', encoding='utf-8')
        collector = threading.Thread(target=old_manager._collect,
            args=(self.job, 'render', process, log, [], 'old-run'), name='old-collector')
        with patch.object(pv, 'load_json', side_effect=slow_old_read):
            collector.start()
            try:
                self.assertTrue(read.wait(5))
                with self.assertRaisesRegex(ValueError, '另一个请求'):
                    self.http.manager.start(self.job, 'render')
            finally:
                release.set(); collector.join(10)
        self.assertFalse(collector.is_alive())
        self.assertEqual(pv.load_json(self.job / 'job.json')['run_id'], 'old-run')
        self.http.manager.start(self.job, 'render')
        self.assertNotEqual(pv.load_json(self.job / 'job.json')['run_id'], 'old-run')

    def test_collector_waits_for_external_transaction_and_finishes_recording(self):
        from studio_process import job_operation
        pv.save_json(self.job / 'job.json', {'status': 'completed', 'run_id': 'old-run'})
        waited = threading.Event()

        def exited():
            waited.set()
            return 0

        process = SimpleNamespace(stdout=io.StringIO(''), wait=exited)
        log = (self.job / 'analysis/old-fixture.log').open('w', encoding='utf-8')
        collector = threading.Thread(target=server.Manager()._collect,
            args=(self.job, 'render', process, log, [], 'old-run'))
        with job_operation(self.job):
            collector.start()
            self.assertTrue(waited.wait(5))
            collector.join(.15)
            self.assertTrue(collector.is_alive(), 'Collector must wait rather than discard the terminal result')
        collector.join(10)
        self.assertFalse(collector.is_alive())
        self.assertEqual(pv.load_json(self.job / 'job.json')['last_exit_code'], 0)


class ServerReuseTests(unittest.TestCase):
    def response(self, release=None):
        return io.BytesIO(json.dumps({'app': server.APP_ID, 'version': server.PROTOCOL_VERSION,
            'release': server.VERSION if release is None else release}).encode())

    def test_same_protocol_different_release_is_not_silently_reused(self):
        with patch.object(sys, 'argv', ['studio_server.py', '--port', '43210', '--no-browser']), \
                patch.object(server, 'urlopen', return_value=self.response('0.0.1')), \
                patch.object(server, 'StudioHTTPServer') as constructor:
            with self.assertRaisesRegex(RuntimeError, '0.0.1'):
                server.main()
        constructor.assert_not_called()

    def test_race_during_bind_preserves_release_mismatch_explanation(self):
        with patch.object(sys, 'argv', ['studio_server.py', '--port', '43210', '--no-browser']), \
                patch.object(server, 'urlopen', side_effect=[OSError('not listening yet'), self.response('0.0.1')]), \
                patch.object(server, 'StudioHTTPServer', side_effect=OSError('already bound')):
            with self.assertRaisesRegex(RuntimeError, '0.0.1'):
                server.main()

    def test_exact_running_release_can_be_reused_in_both_branches(self):
        for race in (False, True):
            with self.subTest(race=race), \
                    patch.object(sys, 'argv', ['studio_server.py', '--port', '43210', '--no-browser']), \
                    patch.object(server, 'urlopen', side_effect=([OSError('not listening yet'), self.response()]
                        if race else [self.response()])), \
                    patch.object(server, 'StudioHTTPServer', side_effect=OSError('already bound')) as constructor, \
                    patch('builtins.print'):
                server.main()
                self.assertEqual(constructor.call_count, int(race))


if __name__ == '__main__':
    unittest.main()
