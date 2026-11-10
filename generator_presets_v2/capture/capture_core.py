"""Filesystem half of Keep Take. No Live imports or GUI code.

Only native Live saves are snapshotted; this module never rewrites an open ALS.
File renames are delegated to the native adapter and verified before completion.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
import zlib
from contextlib import contextmanager
from pathlib import Path


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def clean_name(value):
    value = re.sub(r'[/:\x00-\x1f]', "-", str(value)).strip(" .")
    value = re.sub(r"\s+", " ", value)
    if not value:
        raise ValueError("The take needs a name")
    # Leave space for the unique capture suffix and extension on APFS.
    while len(value.encode("utf-8")) > 150:
        value = value[:-1]
    return value


def read_set(path):
    data = Path(path).read_bytes()
    return data, ET.fromstring(gzip.decompress(data))


def value(node, path):
    found = node.find(path)
    return found.get("Value", "") if found is not None else ""


def saved_audio(root):
    for clip in root.iter("AudioClip"):
        path = value(clip, "SampleRef/FileRef/Path")
        markers = clip.findall("WarpMarkers/WarpMarker")
        bpm = None
        if len(markers) >= 2:
            a, b = markers[:2]
            seconds = float(b.get("SecTime")) - float(a.get("SecTime"))
            beats = float(b.get("BeatTime")) - float(a.get("BeatTime"))
            if seconds > 0 and beats > 0:
                bpm = round(60 * beats / seconds, 6)
        yield dict(path=path, name=value(clip, "Name"), bpm=bpm)


def saved_source(selection, clip, root):
    """Resolve direct recorder routing by saved Track Id, never display text."""
    tracks_node = root.find("LiveSet/Tracks") if root is not None else None
    tracks = list(tracks_node) if tracks_node is not None else []
    index = clip.get("track")
    if not isinstance(index, int) or not 0 <= index < len(tracks):
        return None
    recorder = tracks[index]
    if recorder.tag != "AudioTrack" or not any(
            value(c, "SampleRef/FileRef/Path") == clip["path"] and value(c, "Name") == clip["name"]
            for c in recorder.iter("AudioClip")):
        return None
    route = value(recorder, "DeviceChain/AudioInputRouting/Target")
    match = re.fullmatch(r"AudioIn/Track\.(\d+)/.+", route)
    if not match:
        return None
    indices = [i for i, track in enumerate(tracks) if track.get("Id") == match[1]]
    sources = [s for s in selection.get("sources", []) if s.get("index") in indices]
    if len(indices) == len(sources) == 1:
        return sources[0]
    return None


def rack_label(source):
    tree = source.get("topology", [])
    opti_paths = [r[0] for r in source.get("revisions", [])]
    if not opti_paths:
        opti_paths = [p for p, _id, _class, name in tree if "Optimystery" in name]
    racks = [row for row in tree if row[2] in ("InstrumentGroupDevice", "DrumGroupDevice")]
    candidates = {}
    for opti in opti_paths:
        enclosing = [r for r in racks if len(r[0]) < len(opti) and opti[:len(r[0])] == r[0]]
        if enclosing:
            chosen = min(enclosing, key=lambda r: len(r[0]))
        else:
            following = [r for r in racks if r[0][:-2] == opti[:-2] and r[0][-1] > opti[-1]]
            chosen = min(following, key=lambda r: r[0][-1]) if following else None
        if chosen:
            candidates[tuple(chosen[0])] = chosen[3]
    if len(candidates) == 1:
        return re.sub(r"\s+\d{4}\s+\d{6}$", "", next(iter(candidates.values())))
    return source["name"]


def source_label(selection, clip, saved_root=None, current_setup=False):
    origin = clip.get("origin") or {}
    route = (origin.get("routing", {}).get("input_routing_type") or {}).get("name", "")
    sources = origin.get("sources", [])
    matches = [s for s in sources if s["name"] == route]
    source = saved_source(selection, clip, saved_root) if current_setup else (matches[0] if len(matches) == 1 else None)
    return rack_label(source) if source else clip["name"]


def build_names(selection, saved_root, take_id, name=None, current_setup=False):
    rows = list(saved_audio(saved_root))
    groups = {}
    for clip in selection["clips"]:
        if clip["recording"]:
            raise ValueError("Stop recording before Keep Take")
        source = Path(clip["path"])
        if source.suffix.lower() not in (".wav", ".aif", ".aiff") or not source.is_file():
            raise ValueError("Capture requires an existing recorded WAV or AIFF: " + str(source))
        found = [r for r in rows if r["path"] == str(source) and r["name"] == clip["name"]]
        if not found:
            raise ValueError("The native saved Set does not contain the selected clip; capture stopped")
        group = groups.setdefault(str(source), dict(source=str(source), clip_ids=[], clips=[], bpms=[]))
        group["clip_ids"].append(clip["id"]); group["clips"].append(clip)
        group["bpms"].extend(r["bpm"] for r in found if r["bpm"] is not None)
    names = {}
    for index, group in enumerate(groups.values(), 1):
        label = name or source_label(selection, group["clips"][0], saved_root, current_setup)
        # A scene shares its capture identity; different source files remain distinct.
        if name and len(groups) > 1:
            label += " " + str(index)
        label = re.sub(r"\s+\[opti-[^\]]+\]$", "", label)
        bpms = set(group.pop("bpms"))
        bpm = next(iter(bpms)) if len(bpms) == 1 else None
        if bpm is not None and not re.search(r"\b\d+(?:\.\d+)?\s*bpm\b", label, re.I):
            label += " %g bpm" % bpm
        suffix = take_id + ("-%02d" % index if len(groups) > 1 else "")
        stem = clean_name(label) + " [opti-" + suffix + "]"
        target = Path(group["source"]).with_name(stem + Path(group["source"]).suffix)
        if target.exists():
            raise FileExistsError(str(target))
        group.update(target=str(target), name=stem, bpm=bpm)
        for clip_id in group["clip_ids"]:
            names[clip_id] = stem
    return list(groups.values()), names


def closed_set_references(project, source, active_set):
    """Retain old paths for other Sets/backups, instead of editing closed Sets."""
    found = []
    for path in sorted(Path(project).rglob("*.als")):
        if path == active_set or "Optimystery Takes" in path.parts:
            continue
        try:
            _data, root = read_set(path)
            if any(value(ref, "Path") == source for ref in root.iter("FileRef")):
                found.append(str(path))
        except (OSError, ET.ParseError, gzip.BadGzipFile):
            # Unknown files mean the old path must be retained, not guessed unused.
            found.append(str(path) + " (unreadable)")
    return found


class Capture:
    def __init__(self, bridge, native, clock=time.time, progress=None,
                 wait_clock=time.monotonic, sleep=time.sleep, timing=None):
        self.bridge, self.native, self.clock = bridge, native, clock
        self._progress = progress or (lambda message: None)
        self.wait_clock, self.sleep = wait_clock, sleep
        self.timing = timing or (lambda report: None)
        self.stages, self.operations = [], []
        self._stage = None; self._manifest = None; self._manifest_path = None
        self._restore_token = None

    def close_stage(self):
        if self._stage:
            label, started = self._stage
            self.stages.append(dict(stage=label, seconds=round(self.wait_clock() - started, 6)))
            self._stage = None

    def progress(self, message):
        self.close_stage(); self._stage = (message, self.wait_clock())
        self._progress(message)

    @contextmanager
    def measured(self, operation):
        started = self.wait_clock(); ok = False
        try:
            yield
            ok = True
        finally:
            self.operations.append(dict(operation=operation, seconds=round(self.wait_clock() - started, 6), ok=ok))

    def call(self, operation, **data):
        with self.measured("bridge." + operation):
            return self.bridge.call(operation, **data)

    def native_call(self, operation, *args):
        with self.measured("native." + operation):
            return getattr(self.native, operation)(*args)

    def save_native(self, path):
        path = Path(path)
        def stamp():
            info = path.stat()
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        try:
            before = stamp()
        except FileNotFoundError:
            before = None  # A save may already be replacing the selected Set.
        result = self.native_call("save")
        clean = isinstance(result, dict) and result.get("invoked") is False
        deadline = self.wait_clock() + 20
        last = None; stable = 0
        reason = "the saved file did not change"
        while self.wait_clock() < deadline:
            try:
                observed = stamp()
                if clean or observed != before:
                    stable = stable + 1 if observed == last else 0
                    if stable >= 2:
                        data, root = read_set(path)
                        if stamp() == observed and len(data) == observed[2]:
                            return data, root
                        stable = 0
                    reason = "the saved file is still changing"
                    last = observed
                else:
                    last = None; stable = 0
                    reason = "the saved file did not change"
            except FileNotFoundError:
                last = None; stable = 0
                reason = "the saved file has not reappeared"
            except (EOFError, gzip.BadGzipFile, zlib.error, ET.ParseError):
                last = None; stable = 0
                reason = "the saved file is not yet a complete readable Set"
            self.sleep(.1)
        raise RuntimeError("Live save was not confirmed within 20 seconds: " + reason + ". "
                           "Finish/cancel any Live dialog, then try Keep again.\nSet: " + str(path))

    def run(self, scope="clip", name=None, current_setup=False, target=None):
        started = self.wait_clock(); result = None
        try:
            result = self._run(scope, name, current_setup, target)
            return result
        finally:
            self.close_stage()
            restore = dict(restored=False, reason="older companion or capture did not change view")
            if self._restore_token:
                try:
                    restore = self.call("restore_view", selection=self._restore_token)
                except Exception as error:
                    restore = dict(restored=False, error=str(error))
            report = dict(total_seconds=round(self.wait_clock() - started, 6),
                          stages=self.stages, operations=self.operations, view_restore=restore)
            if self._manifest is not None:
                self._manifest["timings"] = report
                try:
                    write_json(self._manifest_path, self._manifest)
                except OSError as error:
                    report["log_warning"] = str(error)
            self.timing(report)

    def _run(self, scope="clip", name=None, current_setup=False, target=None):
        self.progress("Checking the selected audio and source")
        selection = self.call("inspect", scope=scope, **(target or {}))
        if selection.get("can_restore_view"):
            self._restore_token = selection["selection"]
        active = Path(selection["set_path"])
        if not selection["set_path"] or not active.is_file():
            raise ValueError("Save this Live Set once before using Keep Take")
        source_match = re.fullmatch(r"opti source (\d{8}-\d{6}-\d{3})", active.stem)
        if source_match and (active.parent / "Optimystery Takes" / source_match[1] / "take.json").is_file():
            raise ValueError("This is a retained source Set. Use Live's Save Live Set As to make a working copy before Keep Take, so the original source stays intact.")
        if selection["busy"]:
            raise ValueError("Wait for the current roll to finish")
        self.progress("Capturing Optimystery recall state")
        prepared = self.call("prepare", selection=selection["selection"], current_setup=current_setup)
        exports = []
        for entry in prepared["exports"]:
            path = Path(entry["path"]); deadline = self.clock() + 10
            state = None
            while self.clock() < deadline:
                try:
                    state = json.loads(path.read_text())
                    break
                except (FileNotFoundError, json.JSONDecodeError):
                    time.sleep(.1)  # Max File may be visible before its last write.
            if state is None:
                raise RuntimeError("Optimystery did not export its recall state; use a fresh dev.43 device")
            if state.get("token") != entry["token"] or state.get("error") or state.get("warnings"):
                raise RuntimeError(state.get("error") or "; ".join(state.get("warnings", [])) or "Recall response did not match capture")
            exports.append(dict(entry, content=state))
        self.call("validate", selection=selection["selection"])
        self.progress("Saving the source Set in Live")
        original_data, root = self.save_native(active)
        self.call("validate", selection=selection["selection"])
        take_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock())) + "-%03d" % (int(self.clock() * 1000) % 1000)
        groups, names = build_names(selection, root, take_id, name, current_setup)
        directory = active.parent / "Optimystery Takes" / take_id
        directory.mkdir(parents=True, exist_ok=False)
        snapshot = active.parent / ("opti source " + take_id + ".als")
        if snapshot.exists():
            raise FileExistsError(str(snapshot))
        snapshot.write_bytes(original_data)
        parents = set(re.findall(r"\[opti-(\d{8}-\d{6}-\d{3})(?:-\d{2})?\]", " ".join(c["name"] for c in selection["clips"])))
        parent_set = re.fullmatch(r"opti source (\d{8}-\d{6}-\d{3})", active.stem)
        if parent_set:
            parents.add(parent_set[1])
        manifest = dict(schema=1, id=take_id, status="pending", created=self.clock(),
                        source_set=str(snapshot), active_set=str(active), selection=selection,
                        current_setup=current_setup, exports=exports, audio=groups, parent_ids=sorted(parents),
                        state_moment="Keep Take invocation; not a replay of all mid-recording changes")
        manifest_path = directory / "take.json"
        self._manifest, self._manifest_path = manifest, manifest_path
        write_json(manifest_path, manifest)
        try:
            self.progress("Retaining audio and source backups")
            # Copy audio before native rename for recovery and closed-Set references.
            backup = directory / "Audio"; backup.mkdir()
            for index, group in enumerate(groups):
                source = Path(group["source"])
                group["sha256"] = digest(source)
                group["backup"] = str(backup / ("%03d-" % index + source.name))
                shutil.copy2(source, group["backup"])
                sidecar = Path(str(source) + ".asd")
                if sidecar.exists():
                    shutil.copy2(sidecar, group["backup"] + ".asd")
                # Other Projects can also reference this WAV. Retain the old path
                # for those references; the captured clip uses the newly named file.
                group["retain_original_path"] = True
            write_json(manifest_path, manifest)
            for group_index, group in enumerate(groups):
                self.progress("Naming audio %d of %d in Live" % (group_index + 1, len(groups)))
                old, new = Path(group["source"]), Path(group["target"])
                self.call("focus_clip", selection=selection["selection"], clip=group["clip_ids"][0])
                self.native_call("rename", old.name, new.name, group["clips"][0]["name"])
                deadline = self.clock() + 5
                while not new.is_file() and self.clock() < deadline:
                    time.sleep(.1)
                if not new.is_file() or digest(new) != group["sha256"]:
                    raise RuntimeError("Native file rename did not preserve the recorded audio")
                self.call("verify_file", selection=selection["selection"], old=str(old), new=str(new))
                group["renamed"] = True
                # Old backup Sets remain usable; do not rewrite or reopen them.
                if not old.exists():
                    shutil.copy2(group["backup"], old)
                    if Path(group["backup"] + ".asd").exists():
                        shutil.copy2(group["backup"] + ".asd", str(old) + ".asd")
                    group["old_path_retained"] = True
                write_json(manifest_path, manifest)
            # Do not give clips completed-take names until every native file rename
            # and Live file reference has been verified.
            self.call("rename_clips", selection=selection["selection"], names=names)
            self.call("validate", selection=selection["selection"])
            self.progress("Saving and verifying the linked take")
            final_data, final_root = self.save_native(active)
            self.call("validate", selection=selection["selection"])
            actual = list(saved_audio(final_root))
            for group in groups:
                if not any(r["path"] == group["target"] and r["name"] == group["name"] for r in actual):
                    raise RuntimeError("Final native save does not contain the captured clip/file names")
            temporary = snapshot.with_suffix(".als.tmp")
            temporary.write_bytes(final_data); os.replace(temporary, snapshot)
            manifest.update(status="complete", completed=self.clock(), source_sha256=digest(snapshot))
            write_json(manifest_path, manifest)
            return manifest
        except Exception as exc:
            # Preserve the pre-rename native snapshot's references even on partial UI failure.
            for group in groups:
                old = Path(group["source"])
                if not old.exists() and group.get("backup") and Path(group["backup"]).exists():
                    shutil.copy2(group["backup"], old)
                    if Path(group["backup"] + ".asd").exists():
                        shutil.copy2(group["backup"] + ".asd", str(old) + ".asd")
            manifest.update(status="incomplete", error=str(exc))
            write_json(manifest_path, manifest)
            raise RuntimeError(str(exc) + "\nSource snapshot retained: " + str(snapshot) + "\nDetails: " + str(manifest_path)) from exc


def find_source(selection, index_paths=()):
    paths = {clip["path"] for clip in selection["clips"]}
    names = [clip["name"] for clip in selection["clips"]]
    roots = {Path(selection["set_path"]).parent / "Optimystery Takes"}
    roots.update(Path(path) for path in index_paths)
    found = {}
    for directory in roots:
        for path in directory.glob("*/take.json"):
            try:
                take = json.loads(path.read_text())
                match = any(group["target"] in paths or group["source"] in paths for group in take["audio"])
                match = match or any("[opti-" + take["id"] in name for name in names)
                if match and Path(take["source_set"]).is_file():
                    found[take["id"]] = dict(take, manifest=str(path))
            except (OSError, ValueError, KeyError):
                continue
    if not found:
        raise ValueError("No captured source is linked to this clip. Keep Take links new recordings directly.")
    return list(found.values())
