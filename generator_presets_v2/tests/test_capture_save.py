"""Exercise real temporary ALS files through asynchronous native-save states."""
import gzip
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

BUILD=Path(__file__).resolve().parents[1]
sys.path.insert(0,os.environ.get('CAPTURE_HELPER_DIR',str(BUILD/'capture')))
import capture_core as core
from test_take_capture import Host, xml_file


class Clock:
    def __init__(self):self.now=0.;self.tick=0;self.events={}
    def __call__(self):return self.now
    def sleep(self,seconds):
        self.now+=seconds;self.tick+=1
        if self.tick in self.events:self.events.pop(self.tick)()


class NativeSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.host=Host(self.root);self.clock=Clock();self.calls=0

    def capture(self,save):
        def once():self.calls+=1;return save()
        native=types.SimpleNamespace(save=once,rename=self.host.rename)
        return core.Capture(self.host,native,wait_clock=self.clock,sleep=self.clock.sleep)

    def test_complete_capture_survives_missing_then_partial_files_on_both_saves(self):
        def save():
            data=xml_file(self.host.clips);self.host.saved_bytes=data
            self.host.active.rename(self.root/('backup-%d.als'%self.calls))
            at=self.clock.tick
            self.clock.events[at+2]=lambda:self.host.active.write_bytes(data[:12])
            self.clock.events[at+7]=lambda:self.host.active.write_bytes(data)
            return {'invoked':True}
        original=Path(self.host.clips[0].file_path).read_bytes()
        result=self.capture(save).run(current_setup=True)
        self.assertEqual(self.calls,2)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(Path(result['source_set']).read_bytes(),self.host.active.read_bytes())
        self.assertEqual(Path(result['audio'][0]['target']).read_bytes(),original)

    def test_clean_document_requires_readable_stable_file_without_mtime_change(self):
        before=self.host.active.stat().st_mtime_ns
        data,_root=self.capture(lambda:{'invoked':False}).save_native(self.host.active)
        self.assertEqual(data,self.host.saved_bytes);self.assertEqual(self.calls,1)
        self.assertEqual(before,self.host.active.stat().st_mtime_ns)

    def test_replacement_with_same_size_and_mtime_is_detected(self):
        before=self.host.active.stat();data=self.host.active.read_bytes()
        def save():
            self.host.active.rename(self.root/'old.als')
            self.host.active.write_bytes(data)
            os.utime(self.host.active,ns=(before.st_atime_ns,before.st_mtime_ns))
            return {'invoked':True}
        result,_root=self.capture(save).save_native(self.host.active)
        after=self.host.active.stat()
        self.assertEqual((after.st_size,after.st_mtime_ns),(before.st_size,before.st_mtime_ns))
        self.assertNotEqual(after.st_ino,before.st_ino);self.assertEqual(result,data)

    def test_file_replaced_during_read_is_not_accepted(self):
        self.host.clips[0].name='intermediate';intermediate=xml_file(self.host.clips)
        self.host.clips[0].name='final';final=xml_file(self.host.clips)
        read=core.read_set;reads=[]
        def racing(path):
            result=read(path);reads.append(result[0])
            if len(reads)==1:
                tmp=self.root/'replacement.als';tmp.write_bytes(final);os.replace(tmp,path)
            return result
        def save():self.host.active.write_bytes(intermediate);return {'invoked':True}
        with patch.object(core,'read_set',side_effect=racing):
            data,_root=self.capture(save).save_native(self.host.active)
        self.assertEqual(data,final);self.assertEqual(reads,[intermediate,final]);self.assertEqual(self.calls,1)

    def test_incomplete_xml_retried_until_valid(self):
        data=self.host.active.read_bytes()
        def save():
            self.host.active.write_bytes(gzip.compress(b'<Ableton><LiveSet>'))
            self.clock.events[7]=lambda:self.host.active.write_bytes(data)
            return {'invoked':True}
        result,_root=self.capture(save).save_native(self.host.active)
        self.assertEqual(result,data);self.assertGreaterEqual(self.clock.tick,9)

    def test_unchanged_save_times_out_without_using_old_bytes(self):
        capture=self.capture(lambda:{'invoked':True})
        capture.clock=lambda:-1000000  # wall-clock changes cannot extend the save deadline
        with self.assertRaisesRegex(RuntimeError,'20 seconds.*did not change'):
            capture.save_native(self.host.active)
        self.assertGreaterEqual(self.clock.now,20);self.assertLess(self.clock.now,20.2);self.assertEqual(self.calls,1)

    def test_permanent_missing_and_corrupt_files_timeout_without_snapshot(self):
        for value in (None,b'not gzip',b'\x1f\x8b',gzip.compress(b'<Ableton>')):
            with self.subTest(value=value):
                self.clock=Clock();self.host.save()
                def save():
                    if value is None:self.host.active.unlink()
                    else:self.host.active.write_bytes(value)
                    return {'invoked':True}
                with self.assertRaisesRegex(RuntimeError,'Live save was not confirmed within 20 seconds'):
                    self.capture(save).run(current_setup=True)
                self.assertFalse(list(self.root.glob('opti source*.als')))
                self.assertFalse((self.root/'Optimystery Takes').exists())

    def test_missing_at_start_can_reappear_after_save(self):
        data=self.host.active.read_bytes();self.host.active.unlink()
        def save():
            self.clock.events[2]=lambda:self.host.active.write_bytes(data)
            return {'invoked':False}
        result,_root=self.capture(save).save_native(self.host.active)
        self.assertEqual(result,data)

    def test_cancellation_is_not_swallowed_by_save_retries(self):
        def cancel():raise RuntimeError('Capture device closed; capture cancelled')
        self.clock.events[2]=cancel
        with self.assertRaisesRegex(RuntimeError,'device closed'):
            self.capture(lambda:{'invoked':True}).save_native(self.host.active)
        self.assertEqual(self.calls,1)


if __name__=='__main__':unittest.main()
