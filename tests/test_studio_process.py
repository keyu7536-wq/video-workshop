import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import paper_video as pv
import studio_engine as engine
from studio_process import console_python,run_logged
from studio_server import Manager


class StudioProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.root=Path(self.temporary.name)
        self.job=self.root/'job'; (self.job/'analysis').mkdir(parents=True)
        pv.save_json(self.job/'job.json',{'status':'prepared','output':'keep-existing-video.mp4'})

    def tearDown(self):
        self.temporary.cleanup()

    def test_console_python_stays_in_same_environment_and_requires_sibling(self):
        pythonw=self.root/'pythonw.exe'; pythonw.touch()
        with self.assertRaisesRegex(RuntimeError,'缺少 python.exe'):
            console_python(pythonw)
        python=self.root/'python.exe'; python.touch()
        self.assertEqual(console_python(pythonw),str(python))
        self.assertEqual(console_python(python),str(python))

    def test_child_stderr_reaches_log_live_output_and_error_with_secrets_redacted(self):
        log=self.job/'analysis/render.log'; lines=[]
        command=[console_python(),'-u','-c',
            'import sys; print("progress marker", flush=True); print("private-test-token", file=sys.stderr); raise RuntimeError("具体渲染失败原因")']
        with self.assertRaisesRegex(RuntimeError,'具体渲染失败原因') as raised:
            run_logged(command,log,on_line=lines.append,env={**os.environ,'PAPER_VIDEO_API_KEY':'private-test-token'})
        saved=log.read_text(encoding='utf-8')
        for value in (saved,'\n'.join(lines),str(raised.exception)):
            self.assertIn('具体渲染失败原因',value)
            self.assertNotIn('private-test-token',value)
            self.assertIn('[REDACTED]',value)
        self.assertIn('progress marker',saved)

    @unittest.skipUnless(os.name=='nt','pythonw stream regression is Windows-specific')
    def test_pythonw_parent_captures_grandchild_failure(self):
        pythonw=Path(sys.executable).with_name('pythonw.exe')
        if not pythonw.is_file():self.skipTest('No pythonw in this environment')
        wrapper=self.root/'wrapper.py'; log=self.job/'analysis/pythonw.log'
        wrapper.write_text('from studio_process import console_python,run_logged\nimport sys\n'
            'run_logged([console_python(),"-u","-c","raise RuntimeError(\'grandchild failure marker\')"],sys.argv[1],on_line=print)\n',encoding='utf-8')
        environment={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]),
            'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8'}
        result=subprocess.run([str(pythonw),str(wrapper),str(log)],stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,text=True,encoding='utf-8',env=environment,timeout=15,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertNotEqual(result.returncode,0)
        self.assertIn('grandchild failure marker',result.stdout)
        self.assertIn('grandchild failure marker',log.read_text(encoding='utf-8'))

    def test_render_reports_original_failure_and_preserves_renderer_diagnostic(self):
        diagnostic=self.root/'render_diagnostic.json'
        (self.root/'make_video.py').write_text(
            'from pathlib import Path\nPath("render_diagnostic.json").write_text("diagnostic preserved")\n'
            'raise RuntimeError("shot exceeds available material")\n',encoding='utf-8')
        lines=[]
        with patch.object(engine,'ROOT',self.root),patch('paper_video.log',side_effect=lines.append):
            with self.assertRaisesRegex(RuntimeError,'shot exceeds available material'):
                engine.render(self.job)
        self.assertEqual(diagnostic.read_text(),'diagnostic preserved')
        self.assertIn('shot exceeds available material',(self.job/'analysis/render.log').read_text(encoding='utf-8'))
        self.assertTrue(any('shot exceeds available material' in line for line in lines))

    def start_fixture(self,script,action='render',key=''):
        (self.root/'studio_engine.py').write_text(script,encoding='utf-8')
        manager=Manager()
        with patch('studio_server.ROOT',self.root):manager.start(self.job,action,key)
        manager.worker.join(timeout=15)
        if manager.worker.is_alive():
            manager.process.kill();manager.worker.join(timeout=5)
            self.fail('Fixture worker did not exit')
        return manager,pv.load_json(self.job/'job.json')

    def test_manager_start_failure_is_persisted_without_losing_existing_output(self):
        manager=Manager()
        with patch('studio_server.subprocess.Popen',side_effect=OSError('launch denied')):
            with self.assertRaisesRegex(RuntimeError,'launch denied'):
                manager.start(self.job,'render')
        state=pv.load_json(self.job/'job.json')
        self.assertEqual(state['status'],'failed')
        self.assertEqual(state['last_action'],'render')
        self.assertEqual(state['failed_action'],'render')
        self.assertEqual(state['output'],'keep-existing-video.mp4')
        self.assertIn('launch denied',state['error'])
        self.assertFalse(manager.snapshot()['active'])

    def test_manager_unhandled_exit_recovers_error_state_and_redacts_logs(self):
        manager,state=self.start_fixture('import sys\nprint("private-test-token actual engine failure",file=sys.stderr)\nsys.exit(7)\n',key='private-test-token')
        self.assertEqual(state['status'],'failed');self.assertEqual(state['last_exit_code'],7)
        self.assertEqual(state['last_action'],'render')
        self.assertEqual(state['failed_action'],'render')
        self.assertIn('actual engine failure',state['error'])
        self.assertNotIn('private-test-token',json.dumps(state))
        self.assertNotIn('private-test-token',(self.job/'analysis/studio_run.log').read_text(encoding='utf-8'))
        self.assertEqual(state['output'],'keep-existing-video.mp4')
        self.assertFalse(manager.snapshot()['active'])
        metadata=pv.load_json(self.job/'analysis/worker.json')
        self.assertEqual(metadata['run_id'],state['run_id'])
        self.assertEqual(metadata['worker_pid'],manager.process.pid)
        self.assertEqual(metadata['exit_code'],7)
        self.assertIn('finished_at',metadata)

    def test_manager_preserves_specific_engine_failure(self):
        _,state=self.start_fixture('import json,sys\nfrom pathlib import Path\n'
            'path=Path(sys.argv[2])/"job.json"\nstate=json.loads(path.read_text())\n'
            'state.update(status="failed",error="specific rendering explanation")\n'
            'path.write_text(json.dumps(state))\nsys.exit(1)\n')
        self.assertEqual(state['status'],'failed')
        self.assertEqual(state['error'],'specific rendering explanation')

    def test_manager_zero_exit_without_terminal_state_is_not_success(self):
        _,state=self.start_fixture('print("stopped before work")\n')
        self.assertEqual(state['status'],'failed')
        self.assertIn('stopped before work',state['error'])

    def test_finished_worker_does_not_overwrite_a_different_run(self):
        _,state=self.start_fixture('import json,sys\nfrom pathlib import Path\n'
            'path=Path(sys.argv[2])/"job.json"\nstate=json.loads(path.read_text())\n'
            'state.update(status="processing",run_id="different-run")\n'
            'path.write_text(json.dumps(state))\nsys.exit(7)\n')
        self.assertEqual(state['status'],'processing')
        self.assertEqual(state['run_id'],'different-run')
        self.assertNotIn('last_exit_code',state)

    def test_manager_prepare_accepts_prepared_terminal_state(self):
        state=pv.load_json(self.job/'job.json');state.update(failed_action='export',stage='export',error='old failure')
        pv.save_json(self.job/'job.json',state)
        _,state=self.start_fixture('import json,sys\nfrom pathlib import Path\n'
            'path=Path(sys.argv[2])/"job.json"\nstate=json.loads(path.read_text())\n'
            'state.update(status="prepared",error=None)\npath.write_text(json.dumps(state))\n',action='prepare')
        self.assertEqual(state['status'],'prepared')
        self.assertIsNone(state['error']);self.assertEqual(state['last_exit_code'],0)
        self.assertIsNone(state['failed_action']);self.assertIsNone(state['stage'])

    def test_manager_preserves_successful_plan_gate_without_claiming_worker_failure(self):
        script=('import json,sys\nfrom pathlib import Path\n'
            'path=Path(sys.argv[2])/"job.json"\nstate=json.loads(path.read_text())\n'
            'state.update(status="review_needed",stage="plan_review",error=None)\n'
            'state.pop("failed_action",None)\npath.write_text(json.dumps(state))\n')
        for action in ('prepare','resume','render'):
            with self.subTest(action=action):
                manager,state=self.start_fixture(script,action=action)
                self.assertEqual((state['status'],state['stage']),('review_needed','plan_review'))
                self.assertIsNone(state['error']);self.assertNotIn('failed_action',state)
                self.assertEqual(state['last_exit_code'],0)
                self.assertFalse(manager.snapshot()['active'])


if __name__=='__main__':unittest.main()
