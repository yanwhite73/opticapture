import gzip
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

BUILD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD / "capture"))
sys.path.insert(0, str(BUILD / "remote_script/GeneratorPresetLoaderV2"))
from capture_core import Capture, build_names, digest, find_source, read_set, write_json
from take_capture import TakeCapture


def clip(path, name="recording 1", ident="c1"):
    return types.SimpleNamespace(_live_ptr=ident, file_path=str(path), name=name, is_audio_clip=True,
        is_recording=False, start_marker=0, end_marker=8, loop_start=0, loop_end=8)


def xml_file(clips):
    import xml.etree.ElementTree as E
    root = E.Element("Ableton"); live = E.SubElement(root, "LiveSet")
    for c in clips:
        node = E.SubElement(live, "AudioClip")
        E.SubElement(node, "Name", Value=c.name)
        ref = E.SubElement(E.SubElement(node, "SampleRef"), "FileRef")
        E.SubElement(ref, "Path", Value=c.file_path)
        E.SubElement(ref, "RelativePath", Value="Samples/Recorded/" + Path(c.file_path).name)
        warp = E.SubElement(node, "WarpMarkers")
        E.SubElement(warp, "WarpMarker", SecTime="0", BeatTime="0")
        E.SubElement(warp, "WarpMarker", SecTime="0.576923076923", BeatTime="1")
        E.SubElement(node, "Gain", Value="0.37")
    E.SubElement(live, "OpaquePluginState", Value="native state must remain byte-exact")
    return gzip.compress(E.tostring(root))


class Host:
    def __init__(self, root):
        self.active = root / "music.als"
        audio = root / "Samples/Recorded"; audio.mkdir(parents=True)
        source = audio / "recording.wav"; source.write_bytes(b"unchanged audio\0" * 300)
        Path(str(source) + ".asd").write_bytes(b"analysis")
        self.clips = [clip(source)]
        self.selection = None
        self.failure = None
        self.saved_bytes = None
        self.save()

    def save(self):
        self.saved_bytes = xml_file(self.clips)
        self.active.write_bytes(self.saved_bytes)
        return {"invoked": True}

    def rename(self, old, new, name):
        if self.failure == "before":
            raise RuntimeError("native cancelled")
        source = next(Path(c.file_path) for c in self.clips if Path(c.file_path).name == old)
        target = source.with_name(new)
        source.rename(target)
        if Path(str(source) + ".asd").exists():
            Path(str(source) + ".asd").rename(str(target) + ".asd")
        for c in self.clips:
            if c.file_path == str(source): c.file_path = str(target)
        if self.failure == "after":
            raise RuntimeError("native interrupted after rename")

    def call(self, operation, **request):
        if operation == "inspect":
            self.selection = dict(selection="selected", set_path=str(self.active), busy=False, sources=[],
                clips=[dict(id=c._live_ptr, path=c.file_path, name=c.name, recording=c.is_recording) for c in self.clips])
            return self.selection
        if operation == "prepare": return dict(exports=[], sources=[])
        if operation == "rename_clips":
            for c in self.clips: c.name = request["names"][c._live_ptr]
        if operation == "focus_clip": return {}
        if operation == "verify_file":
            if any(c.file_path == request["old"] for c in self.clips): raise ValueError("stale reference")
        return {}


class CaptureFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.host = Host(self.root)

    def test_keeps_native_set_audio_and_shared_references(self):
        old = Path(self.host.clips[0].file_path); sha = digest(old)
        other = self.root / "arrangement.als"; other.write_bytes(self.host.active.read_bytes())
        other_bytes = other.read_bytes()
        result = Capture(self.host, self.host).run(name="muffle")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(Path(result["source_set"]).read_bytes(), self.host.saved_bytes)
        self.assertEqual(other.read_bytes(), other_bytes)
        self.assertEqual(digest(old), sha)
        self.assertEqual(digest(result["audio"][0]["target"]), sha)
        self.assertTrue(Path(result["audio"][0]["target"] + ".asd").exists())
        self.assertIn("104 bpm", result["audio"][0]["name"])
        self.assertEqual(len(find_source(self.host.call("inspect"))), 1)

    def test_duplicates_share_file_and_name(self):
        self.host.clips.append(clip(self.host.clips[0].file_path, ident="c2"))
        result = Capture(self.host, self.host).run()
        self.assertEqual(len(result["audio"]), 1)
        self.assertEqual(self.host.clips[0].file_path, self.host.clips[1].file_path)
        self.assertEqual(self.host.clips[0].name, self.host.clips[1].name)

    def test_scene_names_distinguish_files(self):
        other = Path(self.host.clips[0].file_path).with_name("second.wav"); other.write_bytes(b"different")
        self.host.clips.append(clip(other, ident="c2"))
        result = Capture(self.host, self.host).run("scene", "scene name")
        self.assertEqual(len(result["audio"]), 2)
        self.assertNotEqual(result["audio"][0]["target"], result["audio"][1]["target"])

    def test_partial_native_failure_retains_original_source_and_manifest(self):
        old = Path(self.host.clips[0].file_path); sha = digest(old)
        self.host.failure = "after"
        with self.assertRaisesRegex(RuntimeError, "Source snapshot retained"):
            Capture(self.host, self.host).run()
        report = json.loads(next(self.root.glob("Optimystery Takes/*/take.json")).read_text())
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(digest(old), sha)
        _data, root = read_set(report["source_set"])
        self.assertEqual(root.find(".//Path").get("Value"), str(old))

    def test_cancel_before_rename_preserves_wav(self):
        old = Path(self.host.clips[0].file_path); sha = digest(old)
        self.host.failure = "before"
        with self.assertRaisesRegex(RuntimeError, "native cancelled"):
            Capture(self.host, self.host).run()
        self.assertEqual(digest(old), sha)

    def test_source_lookup_survives_copy_to_other_project(self):
        take = Capture(self.host, self.host).run()
        selection = self.host.call("inspect")
        selection["set_path"] = str(self.root / "elsewhere/new.als")
        selection["clips"][0]["path"] = "/collected/audio.wav"
        found = find_source(selection, [self.root / "Optimystery Takes"])
        self.assertEqual(found[0]["id"], take["id"])

    def test_set_missing_target_and_recording_are_rejected(self):
        selection = self.host.call("inspect")
        _data, root = read_set(self.host.active)
        selection["clips"][0]["name"] = "changed"
        with self.assertRaisesRegex(ValueError, "saved Set"):
            build_names(selection, root, "one")
        selection["clips"][0]["recording"] = True
        with self.assertRaisesRegex(ValueError, "Stop recording"):
            build_names(selection, root, "one")

    def test_recapture_links_parent_without_overwriting_old_source(self):
        first = Capture(self.host, self.host).run()
        source = Path(first["source_set"]); original = source.read_bytes()
        second = Capture(self.host, self.host).run()
        self.assertEqual(second["parent_ids"], [first["id"]])
        self.assertEqual(source.read_bytes(), original)
        self.assertNotEqual(first["source_set"], second["source_set"])

    def test_bad_portable_export_stops_before_save_or_rename(self):
        path = self.root / "export.json"
        path.write_text(json.dumps(dict(token=3, warnings=["missing LFO target"])))
        call = self.host.call
        def reply(operation, **data):
            if operation == "prepare": return dict(exports=[dict(path=str(path),token=3)])
            return call(operation, **data)
        self.host.call = reply
        original = self.host.active.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "missing LFO target"):
            Capture(self.host, self.host).run()
        self.assertEqual(self.host.active.read_bytes(), original)
        self.assertFalse(list(self.root.glob("opti source*.als")))

    def test_retained_source_must_be_saved_as_working_copy_first(self):
        take = Capture(self.host, self.host).run()
        self.host.active = Path(take["source_set"])
        original = self.host.active.read_bytes()
        with self.assertRaisesRegex(ValueError, "working copy"):
            Capture(self.host, self.host).run()
        self.assertEqual(self.host.active.read_bytes(), original)


class CaptureBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = clip(self.root / "test.wav")
        self.parameter = types.SimpleNamespace(name="Preset Request V2", value=1)
        self.device = types.SimpleNamespace(_live_ptr="d", name="Optimystery", class_name="MxDeviceInstrument", parameters=[self.parameter], chains=[])
        source = types.SimpleNamespace(_live_ptr="t1", name="source", devices=[self.device], has_midi_input=True,
            has_audio_input=False, clip_slots=[], playing_slot_index=-1)
        recorder = types.SimpleNamespace(_live_ptr="t2", name="record", devices=[], has_midi_input=False,
            has_audio_input=True, clip_slots=[types.SimpleNamespace(has_clip=True, clip=self.audio)], playing_slot_index=0)
        self.scene = types.SimpleNamespace(name="scene")
        self.song = types.SimpleNamespace(tracks=[source, recorder], scenes=[self.scene], file_path=str(self.root / "set.als"),
            name="set", tempo=104, signature_numerator=7, signature_denominator=8, is_playing=False,
            view=types.SimpleNamespace(detail_clip=self.audio, selected_scene=self.scene))
        self.surface = types.SimpleNamespace(song=lambda: self.song, _job=None, _transactions={}, log_message=lambda _: None)
        self.bridge = TakeCapture(self.surface, self.root, "test", types.SimpleNamespace(view=None))

    def select(self):
        return self.bridge.inspect(dict(id="selection", scope="clip"))

    def test_observation_binds_source_and_later_roll_is_detected(self):
        self.audio.is_recording = True; self.bridge.observe_recordings()
        self.audio.is_recording = False; self.bridge.observe_recordings()
        selected = self.select()
        self.assertIsNotNone(selected["clips"][0]["origin"])
        self.parameter.value = 2
        with self.assertRaisesRegex(ValueError, "changed since recording"):
            self.bridge.prepare(dict(selection="selection"))

    def test_old_audio_requires_explicit_current_setup(self):
        self.select()
        with self.assertRaisesRegex(ValueError, "no observed source"):
            self.bridge.prepare(dict(selection="selection"))
        with self.assertRaisesRegex(ValueError, "old Optimystery"):
            self.bridge.prepare(dict(selection="selection", current_setup=True))

    def test_name_drift_never_renames_different_target(self):
        self.select(); self.audio.name = "user edit"
        with self.assertRaisesRegex(ValueError, "name changed"):
            self.bridge.rename_clips(dict(selection="selection", names={"c1": "new"}))
        self.assertEqual(self.audio.name, "user edit")

    def test_mid_capture_roll_and_set_change_fail_guard(self):
        selected = self.select()
        self.device.parameters.append(types.SimpleNamespace(name="Capture Request V10",value=0))
        self.bridge.prepare(dict(selection="selection", current_setup=True))
        self.parameter.value = 5
        with self.assertRaisesRegex(ValueError, "changed during capture"):
            self.bridge.rename_clips(dict(selection="selection", names={"c1":"new"}))
        self.assertEqual(self.audio.name, "recording 1")
        self.parameter.value = 1
        self.song.file_path = "/another.als"
        with self.assertRaisesRegex(ValueError, "changed during capture"):
            self.bridge.validate(dict(selection="selection"))

    def test_other_recording_blocks_capture(self):
        self.select()
        self.song.record_mode = True
        with self.assertRaisesRegex(ValueError, "Stop recording"):
            self.bridge.prepare(dict(selection="selection", current_setup=True))

    def test_file_verification_does_not_accept_unchanged_reference(self):
        self.select()
        with self.assertRaisesRegex(ValueError, "did not update"):
            self.bridge.verify_file(dict(selection="selection", old=self.audio.file_path, new="/new.wav"))

    def test_wrong_session_and_stale_requests_are_rejected(self):
        for request in [dict(session="old", time=time.time()), dict(session=self.bridge.session, time=1)]:
            with self.assertRaisesRegex(ValueError, "expired"):
                self.bridge.dispatch(request)

    def test_scene_targets_audio_only(self):
        selected = self.bridge.inspect(dict(id="scene", scope="scene"))
        self.assertEqual(len(selected["clips"]), 1)
        self.assertEqual(selected["clips"][0]["track"], 1)

    def device_target(self, scene=False):
        self.bridge.observe_recordings()
        status = self.bridge.target_status()
        chosen = status["scene"] if scene else status["clip"]
        return dict(id="device-selection", scope="scene" if scene else "clip",
            target_context=status["context"], set_path=self.song.file_path,
            **({"scene_id":chosen["id"]} if scene else {"clip_id":chosen["id"]}),
            expected_clips=chosen["clips"] if scene else [chosen])

    def test_display_target_survives_device_view_and_is_explicit(self):
        target = self.device_target()
        self.song.view.detail_clip = None
        self.bridge.observe_recordings()
        self.assertEqual(self.bridge.target_status()["clip"]["id"], "c1")
        selected = self.bridge.inspect(target)
        self.assertEqual(selected["clips"][0]["id"], "c1")
        # A different live selection cannot silently substitute the displayed target.
        self.song.view.detail_clip = types.SimpleNamespace(is_audio_clip=False)
        self.assertEqual(self.bridge.inspect(target)["clips"][0]["id"], "c1")
        self.assertIsNone(self.bridge.target_status()["clip"])

    def test_deleted_display_target_is_not_replaced(self):
        target = self.device_target()
        self.song.view.detail_clip = None
        self.song.tracks[1].clip_slots[0].has_clip = False
        self.assertIsNone(self.bridge.target_status()["clip"])
        with self.assertRaisesRegex(ValueError, "Select an audio"):
            self.bridge.inspect(target)

    def test_same_path_set_reload_invalidates_display_target(self):
        target = self.device_target()
        self.song.tracks[0]._live_ptr = "new-track"
        with self.assertRaisesRegex(ValueError, "Set changed"):
            self.bridge.inspect(target)

    def test_displayed_clip_name_or_path_drift_rejected(self):
        target = self.device_target()
        for field, value in (("name", "edited"), ("file_path", "/other.wav")):
            old = getattr(self.audio, field); setattr(self.audio, field, value)
            with self.assertRaisesRegex(ValueError, "displayed audio changed"):
                self.bridge.inspect(target)
            setattr(self.audio, field, old)

    def test_scene_identity_survives_selection_change_but_not_content_change(self):
        target = self.device_target(scene=True)
        another = types.SimpleNamespace(name="empty")
        self.song.scenes.append(another); self.song.view.selected_scene = another
        self.assertEqual(self.bridge.inspect(target)["scene"], 0)
        self.audio.name = "changed"
        with self.assertRaisesRegex(ValueError, "displayed audio changed"):
            self.bridge.inspect(target)

    def test_status_poll_publishes_targets_without_export_or_rename(self):
        self.bridge.tick()
        status = json.loads((Path(self.bridge.root) / "status.json").read_text())
        self.assertEqual(status["targets"]["clip"]["name"], self.audio.name)
        self.assertEqual(status["targets"]["scene"]["count"], 1)
        self.assertEqual(self.parameter.value, 1)
        self.assertFalse((Path(self.bridge.root) / "request.json").exists())

    def test_cancelled_request_is_not_dispatched_after_live_resumes(self):
        root = Path(self.bridge.root)
        write_json(root / "request.json", dict(id="cancelled", session=self.bridge.session,
            time=time.time(), operation="inspect"))
        write_json(root / "cancel.json", dict(id="cancelled",session=self.bridge.session))
        self.bridge.tick()
        response = json.loads((root / "response.json").read_text())
        self.assertFalse(response["ok"])
        self.assertIn("cancelled",response["error"])
        self.assertFalse(self.bridge.selections)


if __name__ == "__main__": unittest.main()
