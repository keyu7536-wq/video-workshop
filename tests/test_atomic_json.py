"""Atomic JSON saves tolerate only bounded Windows reader-sharing conflicts."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import paper_video as pv


def windows_error(code=5):
    error=PermissionError(13,'simulated Windows replacement denial')
    error.winerror=code
    return error


class AtomicJSONTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.path=self.root/'state.json';self.path.write_text('{"old": true}',encoding='utf-8')
        self.original=self.path.read_bytes()

    def test_replacement_receives_complete_json_and_same_directory_temp(self):
        replace=Path.replace
        seen=[]
        def inspect(temp,target):
            self.assertEqual(temp.parent,target.parent)
            self.assertEqual(json.loads(temp.read_text(encoding='utf-8')),{'message':'完整内容','items':list(range(50))})
            self.assertEqual(target.read_bytes(),self.original)
            seen.append(temp)
            return replace(temp,target)
        with patch.object(Path,'replace',inspect):
            pv.save_json(self.path,{'message':'完整内容','items':list(range(50))})
        self.assertEqual(len(seen),1)
        self.assertEqual(list(self.root.iterdir()),[self.path])

    def test_concurrent_writers_use_independent_temps_and_leave_one_complete_result(self):
        replace=Path.replace;barrier=threading.Barrier(2,timeout=3);lock=threading.Lock()
        observed=[];errors=[]
        def meet(temp,target):
            with lock:
                first=temp not in observed
                observed.append(temp)
            if first:barrier.wait()
            return replace(temp,target)
        def writer(number):
            try:pv.save_json(self.path,{'writer':number,'payload':[number]*100})
            except Exception as exc:errors.append(exc)
        with patch.object(Path,'replace',meet):
            threads=[threading.Thread(target=writer,args=(i,)) for i in (1,2)]
            for thread in threads:thread.start()
            for thread in threads:thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors,[])
        self.assertEqual(len(set(observed)),2)
        result=pv.load_json(self.path)
        self.assertIn(result['writer'],(1,2));self.assertEqual(result['payload'],[result['writer']]*100)
        self.assertEqual(list(self.root.iterdir()),[self.path])

    def test_known_windows_conflicts_retry_after_full_write_then_succeed(self):
        replace=Path.replace
        for code in (5,32,33):
            with self.subTest(winerror=code):
                calls=[]
                def conflict(temp,target):
                    calls.append(temp)
                    self.assertEqual(pv.load_json(temp),{'new':code})
                    if len(calls)<3:raise windows_error(code)
                    return replace(temp,target)
                with patch.object(pv,'os',SimpleNamespace(name='nt')), \
                     patch.object(Path,'replace',conflict),patch.object(pv.time,'sleep') as sleep:
                    pv.save_json(self.path,{'new':code})
                self.assertEqual(len(calls),3);self.assertEqual(len(set(calls)),1)
                self.assertEqual(sleep.call_count,2)
                self.assertEqual(pv.load_json(self.path),{'new':code})
                self.assertEqual(list(self.root.iterdir()),[self.path])

    def test_persistent_denial_is_bounded_preserves_old_file_and_only_cleans_own_temp(self):
        sibling=self.path.with_suffix('.json.tmp');sibling.write_bytes(b'another writer')
        error=windows_error()
        with patch.object(pv,'os',SimpleNamespace(name='nt')), \
             patch.object(Path,'replace',side_effect=error) as replace,patch.object(pv.time,'sleep') as sleep:
            with self.assertRaises(PermissionError) as raised:pv.save_json(self.path,{'new':True})
        self.assertIs(raised.exception,error)
        self.assertEqual(replace.call_count,7)
        self.assertEqual(sleep.call_count,6)
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list),.51)
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertEqual(sibling.read_bytes(),b'another writer')
        self.assertEqual(set(self.root.iterdir()),{self.path,sibling})

    def test_other_failures_and_non_windows_denials_do_not_retry(self):
        for platform,error in (('posix',windows_error()),('nt',windows_error(2)),('nt',OSError('disk failure'))):
            with self.subTest(platform=platform,error=error),patch.object(pv,'os',SimpleNamespace(name=platform)), \
                 patch.object(Path,'replace',side_effect=error) as replace,patch.object(pv.time,'sleep') as sleep:
                with self.assertRaises(OSError) as raised:pv.save_json(self.path,{'new':True})
                self.assertIs(raised.exception,error)
                replace.assert_called_once();sleep.assert_not_called()
                self.assertEqual(self.path.read_bytes(),self.original)
                self.assertEqual(list(self.root.iterdir()),[self.path])

    def test_unserializable_value_never_touches_original_or_creates_partial_file(self):
        with self.assertRaises(TypeError):pv.save_json(self.path,{'bad':object()})
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertEqual(list(self.root.iterdir()),[self.path])

    @unittest.skipUnless(os.name=='nt','Real sharing rules require Windows')
    def test_real_windows_reader_blocks_replace_then_short_release_allows_save(self):
        candidate=self.root/'control.tmp';candidate.write_bytes(b'control')
        reader=self.path.open('rb')
        try:
            with self.assertRaises(PermissionError) as raised:candidate.replace(self.path)
            self.assertIn(raised.exception.winerror,(5,32,33))
            candidate.unlink()
            closer=threading.Timer(.04,reader.close);closer.start()
            try:pv.save_json(self.path,{'new':'released reader'})
            finally:closer.join(timeout=2)
        finally:reader.close()
        self.assertEqual(pv.load_json(self.path),{'new':'released reader'})
        self.assertEqual(list(self.root.iterdir()),[self.path])

    @unittest.skipUnless(os.name=='nt','Real sharing rules require Windows')
    def test_real_persistent_windows_reader_keeps_original_and_reports_failure(self):
        started=time.monotonic()
        with self.path.open('rb'):
            with self.assertRaises(PermissionError):pv.save_json(self.path,{'new':'blocked reader'})
        self.assertLess(time.monotonic()-started,2)
        self.assertEqual(self.path.read_bytes(),self.original)
        self.assertEqual(list(self.root.iterdir()),[self.path])


if __name__=='__main__':unittest.main()
