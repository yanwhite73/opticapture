"""Real artifact/CLI contracts; native Max/Live behavior remains a separate check."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

BUILD=Path(__file__).resolve().parents[1]
HELPER=Path(os.environ.get('CAPTURE_HELPER_DIR',str(BUILD/'capture')))
sys.path[:0]=[str(BUILD),str(HELPER),str(BUILD.parent/'tools')]
import amxd
import build_capture
from optimystery_capture import target_arguments


class CaptureDeviceTests(unittest.TestCase):
    def test_real_template_audio_graph_and_type_are_preserved(self):
        template=build_capture.resolve_template()
        artifact=BUILD/'capture/device/opticapture.amxd'
        self.assertTrue(amxd.round_trips(template))
        original=amxd.read_patcher(template)['patcher']; built=amxd.read_patcher(artifact)['patcher']
        self.assertEqual([c for c in amxd.read_chunks(template) if c[0]!='ptch'],
                         [c for c in amxd.read_chunks(artifact) if c[0]!='ptch'])
        audio_ids={b['box']['id'] for b in original['boxes'] if b['box'].get('text') in ('plugin~','plugout~')}
        original_audio=[l for l in original['lines'] if l['patchline']['source'][0] in audio_ids]
        self.assertEqual(original_audio,[l for l in built['lines'] if l['patchline']['source'][0] in audio_ids])
        self.assertEqual(built['project']['amxdtype'],original['project']['amxdtype'])
        self.assertEqual(built['latency'],0)
        self.assertEqual(amxd.read_patcher(artifact),json.loads((artifact.with_suffix('.maxpat')).read_text()))

    def test_each_action_is_visible_momentary_and_not_stored(self):
        p=amxd.read_patcher(BUILD/'capture/device/opticapture.amxd')['patcher']
        boxes={b['box'].get('varname'):b['box'] for b in p['boxes']}
        edges={(tuple(l['patchline']['source']),tuple(l['patchline']['destination'])) for l in p['lines']}
        for name in ('keep','scene','find'):
            b=boxes[name]
            self.assertEqual((b['mode'],b['parameter_enable'],b['presentation']),(0,0,1))
            self.assertEqual(boxes[name+'_message']['text'],'action '+name)
            self.assertIn(((b['id'],0),(boxes[name+'_message']['id'],0)),edges)
            self.assertIn(((boxes[name+'_message']['id'],0),(boxes['ui']['id'],0)),edges)
        for b in boxes.values():
            if not b.get('presentation'): continue
            x,y,w,h=b['presentation_rect']
            self.assertGreaterEqual(x,0); self.assertGreaterEqual(y,0)
            self.assertLessEqual(x+w,400); self.assertLessEqual(y+h,168)
        self.assertIn('@autostart 0 @watch 0 @restart 0 @defer 1',boxes['node']['text'])
        self.assertFalse(any(b.get('parameter_enable') for b in boxes.values()))

    def test_target_cannot_inject_a_different_bridge_operation_or_session(self):
        good=dict(target_context=1,set_path='/set.als',clip_id='1',expected_clips=[])
        self.assertEqual(target_arguments(json.dumps(good)),good)
        for change in ({'operation':'rename_clips'},{'session':'fake'},{'scene_id':'2'}):
            with self.assertRaises(ValueError):target_arguments(json.dumps(dict(good,**change)))

    def test_device_pipe_closure_runs_real_capture_recovery(self):
        # Real Python subprocess + stdin EOF + Capture transaction. Only Live is a double.
        with tempfile.TemporaryDirectory() as temp:
            script='''
import os, sys, time, subprocess
from pathlib import Path
sys.path[:0]=[os.environ.get('CAPTURE_HELPER_DIR',sys.argv[1]+"/capture"),sys.argv[1]+"/tests"]
from capture_core import Capture
from optimystery_capture import device_lifetime
from test_take_capture import Host
root=Path(sys.argv[2]); host=Host(root); rename=host.rename
def blocked(*args):
    rename(*args)
    subprocess.run([sys.executable,"-c","import sys,time; from pathlib import Path; Path(sys.argv[1]).write_text('ready'); time.sleep(30)",str(root/"renamed")],check=True)
host.rename=blocked
try:
    with device_lifetime(): Capture(host,host).run()
except Exception as error:
    (root/"error").write_text(str(error))
    sys.exit(2)
'''
            process=subprocess.Popen([sys.executable,'-c',script,str(BUILD),temp],stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.time()+10
                while not (Path(temp)/'renamed').exists() and process.poll() is None and time.time()<deadline:time.sleep(.03)
                self.assertTrue((Path(temp)/'renamed').exists())
                process.stdin.close(); process.stdin=None
                out,err=process.communicate(timeout=8)
                self.assertEqual(process.returncode,2,(out,err))
                report=json.loads(next(Path(temp).glob('Optimystery Takes/*/take.json')).read_text())
                self.assertEqual(report['status'],'incomplete')
                self.assertIn('device closed',report['error'])
                self.assertTrue(Path(report['audio'][0]['source']).is_file())
                self.assertTrue(Path(report['source_set']).is_file())
            finally:
                if process.poll() is None:process.kill();process.wait()

    def test_device_success_and_handled_failure_exit_with_parent_pipe_open(self):
        # communicate() would close stdin and hide the exact bug reported in Live.
        script='''
import os, sys, time
sys.path.insert(0,os.environ.get('CAPTURE_HELPER_DIR',sys.argv[1]+"/capture"))
from optimystery_capture import device_lifetime
try:
    with device_lifetime():
        print("ready",flush=True)
        time.sleep(.15)
        if sys.argv[2]=="error": raise RuntimeError("Accessibility denied")
except RuntimeError as error:
    print(str(error),file=sys.stderr)
    sys.exit(1)
'''
        for outcome,expected in (("success",0),("error",1)):
            with self.subTest(outcome=outcome):
                p=subprocess.Popen([sys.executable,'-c',script,str(BUILD),outcome],stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    self.assertEqual(p.stdout.readline().strip(),'ready')
                    self.assertEqual(p.wait(timeout=5),expected)
                    self.assertFalse(p.stdin.closed)
                    err=p.stderr.read()
                    self.assertNotIn('Fatal Python',err)
                    self.assertNotIn('_enter_buffered_busy',err)
                    if expected:self.assertIn('Accessibility denied',err)
                finally:
                    if p.poll() is None:p.kill();p.wait()
                    p.stdin.close();p.stdout.close();p.stderr.close()

    def test_real_cli_permission_failure_exits_cleanly_before_any_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);native=root/'denied-native'
            native.write_text('#!/usr/bin/python3\nimport sys\nprint("Accessibility permission is required",file=sys.stderr)\nsys.exit(1)\n')
            native.chmod(0o755)
            target=dict(target_context=1,set_path='/not-opened.als',clip_id='1',expected_clips=[])
            p=subprocess.Popen([sys.executable,str(HELPER/'optimystery_capture.py'),'keep','--device',
                '--session','test','--target',json.dumps(target),'--native',str(native),'--library',str(root/'library')],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertEqual(p.wait(timeout=5),1)
                self.assertFalse(p.stdin.closed)
                self.assertIn('Connecting to Live',p.stdout.read())
                err=p.stderr.read();self.assertIn('Accessibility permission',err)
                self.assertNotIn('Fatal Python',err)
                self.assertFalse((root/'library').exists())
            finally:
                if p.poll() is None:p.kill();p.wait()
                p.stdin.close();p.stdout.close();p.stderr.close()

    def test_cli_completion_with_open_pipe_and_prompt_only_on_action(self):
        # Run the real CLI + Capture filesystem transaction against the existing
        # Host double in a temporary project; never contact the user's Live.
        script='''
import os, sys, json
from pathlib import Path
sys.path[:0]=[os.environ.get('CAPTURE_HELPER_DIR',sys.argv[1]+"/capture"),sys.argv[1]+"/tests"]
import optimystery_capture as cli
from test_take_capture import Host
host=Host(Path(sys.argv[2])); host.status={"session":"test"}
def invoke(*args):
    assert args==(("inspect", "--request-access") if sys.argv[3]=="keep" else ("inspect",)), args
    return {"pid":123}
host.invoke=invoke
cli.Native=lambda executable:host
cli.Bridge=lambda library,pid:host
target=dict(target_context=1,set_path=str(host.active),clip_id="c1",expected_clips=[])
sys.exit(cli.main([sys.argv[3],"--device","--session","test","--target",json.dumps(target),
                   "--library",str(Path(sys.argv[2])/"library")]))
'''
        for command in ('keep','inspect'):
            with self.subTest(command=command),tempfile.TemporaryDirectory() as temp:
                p=subprocess.Popen([sys.executable,'-c',script,str(BUILD),temp,command],stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    code=p.wait(timeout=8)
                    out,err=p.stdout.read(),p.stderr.read()
                    self.assertEqual(code,0,(out,err));self.assertFalse(p.stdin.closed)
                    self.assertEqual(err,'')
                    if command=='keep':
                        self.assertIn('"type": "complete"',out)
                        report=json.loads(next(Path(temp).glob('Optimystery Takes/*/take.json')).read_text())
                        self.assertEqual(report['status'],'complete')
                    else:
                        self.assertFalse((Path(temp)/'Optimystery Takes').exists())
                finally:
                    if p.poll() is None:p.kill();p.wait()
                    p.stdin.close();p.stdout.close();p.stderr.close()


if __name__=='__main__':unittest.main()
