import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as E

BUILD = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("CAPTURE_HELPER_DIR", str(BUILD / "capture")))
from capture_core import Capture, build_names, source_label
from test_take_capture import Host


def source(index=0, name="opti ambient fm sweep 1"):
    rack = ["devices", 1]
    opti = rack + ["chains", 0, "devices", 0]
    return dict(index=index, name="1-" + name, revisions=[[opti, []]], topology=[
        [["devices", 0], "m", "MidiEffectGroupDevice", "MIDI Effect Rack"],
        [rack, "r", "InstrumentGroupDevice", name],
        [opti, "o", "MxDeviceMidiEffect", "Optimystery"],
        [rack + ["chains", 0, "devices", 1], "g", "InstrumentGroupDevice", "Evolupad 1109 221144"]])


def saved(clip, route="AudioIn/Track.12/TrackOut", source_id="12"):
    root = E.Element("Ableton"); tracks = E.SubElement(E.SubElement(root, "LiveSet"), "Tracks")
    E.SubElement(tracks, "MidiTrack", Id=source_id)
    recorder = E.SubElement(tracks, "AudioTrack", Id="19")
    routing = E.SubElement(E.SubElement(recorder, "DeviceChain"), "AudioInputRouting")
    E.SubElement(routing, "Target", Value=route)
    E.SubElement(routing, "UpperDisplayString", Value="1-opti ambient fm sweep")
    c = E.SubElement(recorder, "AudioClip"); E.SubElement(c, "Name", Value=clip["name"])
    ref = E.SubElement(E.SubElement(c, "SampleRef"), "FileRef")
    E.SubElement(ref, "Path", Value=clip["path"])
    warp = E.SubElement(c, "WarpMarkers")
    E.SubElement(warp, "WarpMarker", SecTime="0", BeatTime="0")
    E.SubElement(warp, "WarpMarker", SecTime=str(60/73), BeatTime="1")
    return root


class NamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "Ambient 0002 [2026-11-09 221623].wav"
        self.path.write_bytes(b"audio")
        self.clip = dict(id="c", track=1, name="Ambient 2", path=str(self.path), recording=False, origin=None)
        self.selection = dict(clips=[self.clip], sources=[source()])

    def test_unobserved_current_setup_uses_routed_enclosing_rack(self):
        groups, names = build_names(self.selection, saved(self.clip), "new", current_setup=True)
        self.assertEqual(names["c"], "opti ambient fm sweep 1 73 bpm [opti-new]")
        self.assertEqual(Path(groups[0]["target"]).stem, names["c"])

    def test_explicit_current_ignores_stale_observed_origin(self):
        old = source(name="old preset")
        self.clip["origin"] = dict(sources=[old], routing={"input_routing_type": {"name": old["name"]}})
        self.assertEqual(source_label(self.selection, self.clip, saved(self.clip), True), "opti ambient fm sweep 1")
        self.assertEqual(source_label(self.selection, self.clip, saved(self.clip)), "old preset")

    def test_does_not_guess_only_source_for_external_or_invalid_route(self):
        for route in ("AudioIn/External/S0", "AudioIn/Track.99/TrackOut", "", "AudioIn/Main"):
            with self.subTest(route=route):
                self.assertEqual(source_label(self.selection, self.clip, saved(self.clip, route), True), "Ambient 2")
        self.assertEqual(source_label(self.selection, self.clip, saved(self.clip)), "Ambient 2")

    def test_track_id_not_truncated_name_or_first_source(self):
        self.selection["sources"].insert(0, source(index=4, name="wrong first preset"))
        self.assertEqual(source_label(self.selection, self.clip, saved(self.clip), True), "opti ambient fm sweep 1")

    def test_missing_or_wrong_recorder_does_not_cross_match_other_track(self):
        root = saved(self.clip)
        for change in ({"track": 0}, {"track": 9}, {"name": "different"}, {"path": "/different.wav"}):
            row = dict(self.clip, **change)
            self.assertEqual(source_label(self.selection, row, root, True), row["name"])

    def test_top_level_opti_uses_following_instrument_rack_not_midi_effect(self):
        s = self.selection["sources"][0]
        s["revisions"] = [[["devices", 0], []]]
        s["topology"] = [
            [["devices", 0], "o", "MxDeviceMidiEffect", "Optimystery"],
            [["devices", 1], "m", "MidiEffectGroupDevice", "MIDI Effect Rack"],
            [["devices", 2], "r", "InstrumentGroupDevice", "Evolupad 1109 221144"]]
        self.assertEqual(source_label(self.selection, self.clip, saved(self.clip), True), "Evolupad")

    def test_explicit_name_still_overrides_source_and_bpm_not_duplicated(self):
        groups, _ = build_names(self.selection, saved(self.clip), "new", "custom 73 bpm", True)
        self.assertEqual(groups[0]["name"], "custom 73 bpm [opti-new]")

    def test_failed_file_rename_leaves_original_clip_name(self):
        for failure in ("before", "after"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                host = Host(Path(directory)); original = host.clips[0].name
                host.failure = failure
                with self.assertRaisesRegex(RuntimeError, "native"):
                    Capture(host, host).run(name="source preset")
                self.assertEqual(host.clips[0].name, original)

    def test_all_scene_files_verified_before_any_clip_names_change(self):
        with tempfile.TemporaryDirectory() as directory:
            host = Host(Path(directory)); c = copy.copy(host.clips[0]); c._live_ptr = "c2"
            p = Path(c.file_path).with_name("second.wav"); p.write_bytes(b"second"); c.file_path = str(p)
            host.clips.append(c); originals = [c.name for c in host.clips]
            rename = host.rename; calls = []
            def fail_second(old, new, name):
                calls.append(old)
                if len(calls) == 2: raise RuntimeError("second file unavailable")
                rename(old, new, name)
            host.rename = fail_second
            with self.assertRaisesRegex(RuntimeError, "second file"):
                Capture(host, host).run("scene", name="source preset")
            self.assertEqual([c.name for c in host.clips], originals)


if __name__ == "__main__": unittest.main()
