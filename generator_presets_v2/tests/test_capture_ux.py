import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest

BUILD = Path(__file__).resolve().parents[1]
sys.path[:0] = [os.environ.get('CAPTURE_HELPER_DIR', str(BUILD/'capture')),
               os.environ.get('CAPTURE_COMPANION_DIR', str(BUILD/'remote_script/GeneratorPresetLoaderV2'))]
from capture_core import Capture
from take_capture import TakeCapture
import test_take_capture as fixtures
Host = fixtures.Host


class View:
    def __init__(self, detail=True, device=True, browser=False):
        self.visible = {'Detail':detail,'Detail/Clip':detail and not device,
                        'Detail/DeviceChain':detail and device,'Browser':browser}
        self.focused_document_view = 'Session'; self.calls=[]
    def is_view_visible(self, name): return self.visible[name]
    def show_view(self, name):
        self.calls.append(('show',name));self.visible[name]=True
        if name.startswith('Detail/'):
            self.visible['Detail']=True
            self.visible['Detail/Clip']=name=='Detail/Clip'
            self.visible['Detail/DeviceChain']=name=='Detail/DeviceChain'
    def hide_view(self, name):
        self.calls.append(('hide',name));self.visible[name]=False
        if name=='Detail': self.visible['Detail/Clip']=self.visible['Detail/DeviceChain']=False
    def focus_view(self, name): self.show_view(name);self.calls.append(('focus',name))


class RestoreTests(unittest.TestCase):
    def setUp(self):
        fixtures.CaptureBridgeTests.setUp(self)
        self.device.parameters.append(types.SimpleNamespace(name='Capture Request V10',value=0))
    def prepare(self, view):
        self.bridge.application.view=view
        self.bridge.inspect(dict(id='selection',scope='clip',clip_id=self.audio._live_ptr))
        self.bridge.prepare(dict(selection='selection',current_setup=True))
        self.bridge.focus_clip(dict(selection='selection',clip=self.audio._live_ptr))
        view.show_view('Browser')
    def test_restores_device_clip_hidden_detail_and_browser_visibility(self):
        for detail,device,browser in [(True,True,False),(True,False,True),(False,False,False)]:
            with self.subTest(detail=detail,device=device,browser=browser):
                view=View(detail,device,browser);before=dict(view.visible)
                self.prepare(view)
                self.assertTrue(self.bridge.restore_view(dict(selection='selection'))['restored'])
                self.assertEqual({k:view.visible[k] for k in before},before)
                self.assertFalse(self.bridge.restore_view(dict(selection='selection'))['restored'])
    def test_restores_prior_detail_clip_after_scene_capture_focus(self):
        view=View();self.song.view.detail_clip=None
        self.prepare(view)
        self.bridge.restore_view(dict(selection='selection'))
        self.assertIsNone(self.song.view.detail_clip)
    def test_changed_set_does_not_restore_prior_objects(self):
        view=View();self.prepare(view);view.calls=[]
        self.song.file_path='/another.als'
        self.assertEqual(self.bridge.restore_view(dict(selection='selection'))['reason'],'Set changed')
        self.assertFalse(view.calls)
    def test_user_changed_clip_is_not_overridden(self):
        view=View();self.prepare(view);view.calls=[]
        other=object();self.song.view.detail_clip=other
        self.assertEqual(self.bridge.restore_view(dict(selection='selection'))['reason'],'clip selection changed')
        self.assertIs(self.song.view.detail_clip,other);self.assertFalse(view.calls)
    def test_no_focus_change_does_not_restore(self):
        self.bridge.application.view=View()
        self.bridge.inspect(dict(id='selection',scope='clip'))
        self.bridge.prepare(dict(selection='selection',current_setup=True))
        self.assertFalse(self.bridge.restore_view(dict(selection='selection'))['restored'])


class TransactionUXTests(unittest.TestCase):
    def test_success_and_failure_restore_and_persist_stage_operation_timings(self):
        for failure in (None,'before'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as tmp:
                host=Host(Path(tmp));host.failure=failure;call=host.call;events=[];reports=[]
                def bridge(operation,**data):
                    events.append(operation)
                    if operation=='restore_view': return dict(restored=True)
                    result=call(operation,**data)
                    if operation=='inspect':result['can_restore_view']=True
                    return result
                host.call=bridge;capture=Capture(host,host,timing=reports.append)
                if failure:
                    with self.assertRaisesRegex(RuntimeError,'native cancelled'):capture.run()
                else: capture.run()
                self.assertEqual(events[-1],'restore_view')
                record=json.loads(next(Path(tmp).glob('Optimystery Takes/*/take.json')).read_text())
                self.assertEqual(record['timings'],reports[0]);self.assertTrue(reports[0]['view_restore']['restored'])
                self.assertTrue(reports[0]['stages']);self.assertGreater(reports[0]['total_seconds'],0)
                rename=[x for x in reports[0]['operations'] if x['operation']=='native.rename'][0]
                self.assertEqual(rename['ok'],not failure)
    def test_restore_failure_does_not_erase_verified_capture_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            host=Host(Path(tmp));call=host.call
            def bridge(operation,**data):
                if operation=='restore_view':raise RuntimeError('view unavailable')
                result=call(operation,**data)
                if operation=='inspect':result['can_restore_view']=True
                return result
            host.call=bridge;result=Capture(host,host).run()
            self.assertEqual(result['status'],'complete')
            self.assertEqual(result['timings']['view_restore']['error'],'view unavailable')
    def test_older_companion_remains_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            host=Host(Path(tmp));call=host.call
            def bridge(operation,**data):
                self.assertNotEqual(operation,'restore_view');return call(operation,**data)
            host.call=bridge
            self.assertEqual(Capture(host,host).run()['status'],'complete')

if __name__=='__main__':unittest.main()
