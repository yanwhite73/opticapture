"""User-invoked take capture bridge. No socket, subprocess or audio mutation.

The desktop command performs native saving/Browser renaming. This module reads
selection, records origin observations, changes explicitly requested clip names,
and requests the Max device's native portable-state cache before saving.
"""
import json
import os
import time


SCHEMA = 1
REQUEST_PARAMETER = "Capture Request V10"


def atomic_json(path, value):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=True, separators=(",", ":"))
        handle.flush()
    os.replace(temporary, path)


def read_json(path, limit=4194304):
    if os.path.getsize(path) > limit:
        raise ValueError("Capture message too large")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def identity(obj):
    return str(getattr(obj, "_live_ptr", id(obj)))


def devices(container, prefix=(), depth=0):
    if depth > 12:
        raise ValueError("Device nesting exceeds capture limit")
    for index, device in enumerate(container.devices):
        path = prefix + ("devices", index)
        yield path, device
        for chain_index, chain in enumerate(getattr(device, "chains", ())):
            for entry in devices(chain, path + ("chains", chain_index), depth + 1):
                yield entry


def topology(track):
    return [(list(path), identity(device), str(device.class_name), str(device.name))
            for path, device in devices(track)]


def routing(track):
    result = {}
    for name in ("input_routing_type", "input_routing_channel", "output_routing_type", "output_routing_channel"):
        try:
            value = getattr(track, name)
            result[name] = {"name": str(getattr(value, "display_name", value)),
                            "identifier": str(getattr(value, "identifier", ""))}
        except Exception:
            result[name] = None
    return result


class TakeCapture:
    def __init__(self, surface, library, version, application, clock=time.time):
        if not library or not os.path.isabs(str(library)):
            raise ValueError("Capture requires the companion inside the User Library's Remote Scripts folder")
        self.surface, self.library, self.version = surface, str(library), version
        self.application, self.clock = application, clock
        self.root = os.path.join(self.library, "Optimystery Capture", "Bridge", str(os.getpid()))
        os.makedirs(self.root, exist_ok=True)
        self.session = str(os.getpid()) + "-" + str(int(clock() * 1000000))
        self.last_request = None
        self.last_status = 0
        self.last_observation = 0
        self.origins = {}
        self.recording = set()
        self.selections = {}
        self.prepared = {}
        self.set_identity = None
        self.target_clip = None
        self.context = 0
        self.counter = int(clock() * 1000) % 8000000

    def song(self):
        return self.surface.song()

    def busy(self):
        return bool(getattr(self.surface, "_job", None) or getattr(self.surface, "_transactions", None))

    def source_tracks(self):
        result = []
        for index, track in enumerate(self.song().tracks):
            if not getattr(track, "has_midi_input", False):
                continue
            tree = topology(track)
            revisions = []
            for path, device in devices(track):
                controls = {str(p.name): p for p in device.parameters}
                if "Preset Request V2" in controls:
                    revisions.append((list(path), [(name, float(controls[name].value)) for name in
                        ("Preset Request V2", "Commit Complete V4") if name in controls]))
            result.append(dict(index=index, id=identity(track), name=str(track.name), topology=tree,
                               routing=routing(track), playing_slot=int(track.playing_slot_index), revisions=revisions))
        return result

    def observe_recordings(self):
        song = self.song()
        marker = (str(song.file_path), tuple(identity(t) for t in song.tracks))
        if self.set_identity != marker:
            self.origins.clear(); self.recording.clear(); self.selections.clear(); self.prepared.clear()
            self.set_identity = marker
            self.target_clip = None
            self.context += 1
        detail = song.view.detail_clip
        if detail is not None:
            # Device View may clear detail_clip. Keep the last audio choice only
            # in that case; selecting a MIDI clip explicitly clears the target.
            self.target_clip = identity(detail) if detail.is_audio_clip else None
        recording = set()
        for track_index, track in enumerate(song.tracks):
            if not getattr(track, "has_audio_input", False):
                continue
            candidates = []
            index = int(track.playing_slot_index)
            if 0 <= index < len(track.clip_slots):
                slot = track.clip_slots[index]
                if slot.has_clip:
                    candidates.append(slot.clip)
            if getattr(song, "record_mode", False):
                candidates.extend(getattr(track, "arrangement_clips", ()))
            for clip in candidates:
                if not clip.is_recording:
                    continue
                key = identity(clip)
                recording.add(key)
                if key not in self.recording:
                    self.origins[key] = dict(observed_at=self.clock(), recording_track=track_index,
                        routing=routing(track), sources=self.source_tracks(), busy=self.busy())
        self.recording = recording

    def clip_entry(self, clip, track_index, slot=None, arrangement=None):
        return dict(id=identity(clip), track=track_index, slot=slot, arrangement=arrangement,
                    name=str(clip.name), path=str(clip.file_path),
                    recording=bool(clip.is_recording),
                    start=float(clip.start_marker), end=float(clip.end_marker),
                    loop_start=float(clip.loop_start), loop_end=float(clip.loop_end),
                    origin=self.origins.get(identity(clip)))

    def all_audio(self):
        for ti, track in enumerate(self.song().tracks):
            for si, slot in enumerate(track.clip_slots):
                if slot.has_clip and slot.clip.is_audio_clip:
                    yield slot.clip, self.clip_entry(slot.clip, ti, si)
            for ai, clip in enumerate(getattr(track, "arrangement_clips", ())):
                if clip.is_audio_clip:
                    yield clip, self.clip_entry(clip, ti, arrangement=ai)

    def inspect(self, request):
        song = self.song()
        self.observe_recordings()
        if "target_context" in request and (request["target_context"] != self.context or
                request.get("set_path") != str(song.file_path)):
            raise ValueError("The displayed Set changed. Select the target again.")
        scope = request.get("scope", "clip")
        if scope not in ("clip", "scene"):
            raise ValueError("Choose clip or scene")
        scene = list(song.scenes).index(song.view.selected_scene) if scope == "scene" else None
        selected = song.view.detail_clip if scope == "clip" else None
        if scope == "scene" and "scene_id" in request:
            scene = next((i for i, s in enumerate(song.scenes) if identity(s) == request["scene_id"]), None)
            if scene is None:
                raise ValueError("The displayed scene no longer exists")
        matches = [(clip, row) for clip, row in self.all_audio()
                   if ((identity(clip) == request["clip_id"] if "clip_id" in request else clip == selected)
                       if scope == "clip" else row["slot"] == scene)]
        if not matches:
            raise ValueError("Select an audio clip" if scope == "clip" else "The selected scene has no audio clips")
        if len(matches) > 128:
            raise ValueError("Capture is limited to 128 audio clips")
        if "expected_clips" in request:
            actual = sorted((r["id"], r["path"], r["name"]) for _, r in matches)
            expected = sorted((r["id"], r["path"], r["name"]) for r in request["expected_clips"])
            if actual != expected:
                raise ValueError("The displayed audio changed. Check the target and try again.")
        token = request["id"]
        # Store live references so reordering/deletion cannot retarget a later request.
        self.selections[token] = matches
        if len(self.selections) > 8:
            expired = next(iter(self.selections))
            del self.selections[expired]
            self.prepared.pop(expired, None)
        return dict(selection=token, set_path=str(song.file_path), set_name=str(song.name),
                    scope=scope, scene=scene, scene_name=str(song.scenes[scene].name) if scene is not None else None,
                    tempo=float(song.tempo), meter=[int(song.signature_numerator), int(song.signature_denominator)],
                    clips=[row for _clip, row in matches], sources=self.source_tracks(),
                    busy=self.busy(), playing=bool(song.is_playing),
                    can_restore_view=callable(getattr(self.application.view, "is_view_visible", None)),
                    session=self.session, runtime=self.version)

    def selected(self, request, check_name=True):
        self.validate(request)
        matches = self.selections.get(request.get("selection"))
        if not matches:
            raise ValueError("Selection expired; run the command again")
        for clip, row in matches:
            if identity(clip) != row["id"] or str(clip.file_path) != row["path"] or clip.is_recording:
                raise ValueError("Selected audio changed or is recording")
            if check_name and str(clip.name) != row["name"]:
                raise ValueError("Clip name changed during capture")
        return matches

    @staticmethod
    def stable_sources(sources):
        return [{k: v for k, v in source.items() if k != "playing_slot"} for source in sources]

    def validate(self, request):
        guard = self.prepared.get(request.get("selection"))
        if guard and (str(self.song().file_path) != guard["set_path"] or self.busy()
                      or self.stable_sources(self.source_tracks()) != guard["sources"]):
            raise ValueError("Source or Set changed during capture; no completion was assumed")
        if getattr(self.song(), "record_mode", False) or any(c.is_recording for c, _ in self.all_audio()):
            raise ValueError("Stop recording before capture")
        return dict(valid=True)

    def prepare(self, request):
        matches = self.selected(request)
        if self.busy():
            raise ValueError("Wait for the current roll to finish")
        now_sources = self.source_tracks()
        explicit = bool(request.get("current_setup"))
        for _clip, row in matches:
            origin = row.get("origin")
            if not explicit and (not origin or origin.get("busy")):
                raise ValueError("This recording has no observed source. If the correct setup is loaded, enable Use current setup and try again (command line: --current-setup).")
            if not explicit and origin["sources"] != now_sources:
                # Playing-slot changes after stop are normal; topology/routing changes are not.
                if self.stable_sources(origin["sources"]) != self.stable_sources(now_sources):
                    raise ValueError("Source devices/routing changed since recording. Restore the source, or explicitly capture --current-setup.")
        view = self.remember_view()
        exports = []
        for ti, track in enumerate(self.song().tracks):
            for path, device in devices(track):
                parameters = {str(p.name): p for p in device.parameters}
                if "Preset Request V2" not in parameters:
                    continue
                if REQUEST_PARAMETER not in parameters:
                    raise ValueError("Replace the old Optimystery instance with dev.43 before capturing portable state")
                self.counter = self.counter % 8388606 + 1
                if self.counter == int(parameters[REQUEST_PARAMETER].value):
                    self.counter = self.counter % 8388606 + 1
                token = self.counter
                filename = os.path.join(self.library, "Max MIDI Effect", "Optimystery", "capture-state-%d.json" % token)
                if os.path.exists(filename):
                    os.unlink(filename)
                parameters[REQUEST_PARAMETER].value = token
                exports.append(dict(track=ti, device=list(path), token=token, path=filename))
        self.prepared[request["selection"]] = dict(set_path=str(self.song().file_path), sources=self.stable_sources(now_sources), view=view)
        return dict(exports=exports, sources=now_sources, current_setup=explicit,
                    set_path=str(self.song().file_path))

    def rename_clips(self, request):
        matches = self.selected(request)
        names = request.get("names", {})
        if set(names) != {row["id"] for _clip, row in matches}:
            raise ValueError("Names do not match the captured selection")
        if any(not isinstance(name, str) or not name.strip() or len(name) > 220 or "\x00" in name for name in names.values()):
            raise ValueError("Invalid clip name")
        changed = []
        try:
            for clip, row in matches:
                old = str(clip.name); clip.name = names[row["id"]]
                changed.append((clip, old))
                if str(clip.name) != names[row["id"]]:
                    raise ValueError("Clip rename did not read back")
        except Exception:
            for clip, old in reversed(changed):
                clip.name = old
            raise
        for clip, row in matches:
            row["name"] = str(clip.name)
        return dict(names={row["id"]: str(clip.name) for clip, row in matches})

    def focus_clip(self, request):
        matches = self.selected(request)
        target = next((clip for clip, row in matches if row["id"] == request.get("clip")), None)
        if target is None:
            raise ValueError("Unknown capture clip")
        self.song().view.detail_clip = target
        self.application.view.show_view("Detail/Clip")
        self.application.view.focus_view("Detail/Clip")
        guard = self.prepared.get(request.get("selection"))
        if guard is not None:
            guard["focused_clip"] = target
        return dict(name=str(target.name), path=str(target.file_path))

    def remember_view(self):
        view = self.application.view
        if not callable(getattr(view, "is_view_visible", None)):
            return None
        return dict(visible={name: bool(view.is_view_visible(name)) for name in
                    ("Detail", "Detail/Clip", "Detail/DeviceChain", "Browser")},
                    detail_clip=self.song().view.detail_clip,
                    document=str(view.focused_document_view))

    def restore_view(self, request):
        guard = self.prepared.get(request.get("selection"))
        if not guard or not guard.get("view") or "focused_clip" not in guard:
            return dict(restored=False, reason="no capture view change")
        state = guard.pop("view")  # Cleanup is once-only, including errors.
        song = self.song()
        if str(song.file_path) != guard["set_path"]:
            return dict(restored=False, reason="Set changed")
        if song.view.detail_clip != guard["focused_clip"]:
            return dict(restored=False, reason="clip selection changed")
        view = self.application.view; visible = state["visible"]
        song.view.detail_clip = state["detail_clip"]
        if visible["Detail"]:
            panel = "Detail/DeviceChain" if visible["Detail/DeviceChain"] else "Detail/Clip"
            view.show_view(panel); view.focus_view(panel)
        else:
            view.hide_view("Detail")
            if state["document"] in ("Session", "Arranger"):
                view.focus_view(state["document"])
        (view.show_view if visible["Browser"] else view.hide_view)("Browser")
        return dict(restored=True)

    def verify_file(self, request):
        self.validate(request)
        matches = self.selections.get(request.get("selection"), [])
        old, new = request["old"], request["new"]
        targeted = [(clip, row) for clip, row in matches if row["path"] == old]
        if not targeted:
            raise ValueError("File is not in the captured selection")
        if any(str(clip.file_path) != new for clip, _row in targeted):
            raise ValueError("Live did not update the selected audio reference")
        stale = [row for _clip, row in self.all_audio() if row["path"] == old]
        if stale:
            raise ValueError("Other clips in this Set still reference the old filename")
        for clip, row in targeted:
            row["path"] = new
        return dict(updated=len(targeted))

    def dispatch(self, request):
        if request.get("session") != self.session or self.clock() - float(request.get("time", 0)) > 60:
            raise ValueError("Capture request expired or belongs to another Live session")
        operations = {"inspect": self.inspect, "prepare": self.prepare, "rename_clips": self.rename_clips,
                      "focus_clip": self.focus_clip, "verify_file": self.verify_file, "validate": self.validate,
                      "restore_view": self.restore_view}
        action = operations.get(request.get("operation"))
        if action is None:
            raise ValueError("Unknown capture operation")
        return action(request)

    def target_status(self):
        song = self.song()
        scenes = list(song.scenes)
        scene_index = scenes.index(song.view.selected_scene) if song.view.selected_scene in scenes else None
        selected, members = None, []
        for _clip, row in self.all_audio():
            compact = {key: row[key] for key in ("id", "path", "name", "recording")}
            if row["id"] == self.target_clip:
                selected = dict(compact, track_name=str(song.tracks[row["track"]].name))
            if scene_index is not None and row["slot"] == scene_index:
                members.append(compact)
        if selected is None:
            self.target_clip = None
        scene = None if scene_index is None else dict(id=identity(scenes[scene_index]),
                    name=str(scenes[scene_index].name), count=len(members), clips=members[:128])
        return dict(protocol=1, context=self.context, clip=selected, scene=scene,
                    busy=self.busy(), recording=bool(self.recording or getattr(song, "record_mode", False)))

    def tick(self):
        now = self.clock()
        if now - self.last_observation >= .25:
            self.last_observation = now
            try:
                self.observe_recordings()
            except Exception as exc:
                self.surface.log_message("Optimystery Capture: observation unavailable: %s" % exc)
        if now - self.last_status > .5:
            self.last_status = now
            atomic_json(os.path.join(self.root, "status.json"), dict(schema=SCHEMA, session=self.session,
                        pid=os.getpid(), updated=now, version=self.version, set_path=str(self.song().file_path),
                        targets=self.target_status()))
        path = os.path.join(self.root, "request.json")
        if not os.path.isfile(path):
            return
        request = read_json(path)
        if request.get("id") == self.last_request:
            return
        self.last_request = request.get("id")
        response = dict(id=self.last_request, session=self.session)
        try:
            cancel_path = os.path.join(self.root, "cancel.json")
            if os.path.isfile(cancel_path):
                cancelled = read_json(cancel_path)
                if cancelled.get("session") == self.session and cancelled.get("id") == self.last_request:
                    raise ValueError("Capture request was cancelled")
            response.update(ok=True, result=self.dispatch(request))
        except Exception as exc:
            response.update(ok=False, error=str(exc))
        atomic_json(os.path.join(self.root, "response.json"), response)
