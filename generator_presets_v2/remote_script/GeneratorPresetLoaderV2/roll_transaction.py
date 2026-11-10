"""Request ownership for generation on the existing track. No temporary tracks or transfers.

A fresh grant protects preflight. Once generation starts the owned device suffix is replaced,
following the original workflow. MIDI stays unchanged until Max receives a successful completion.
This is deliberately not a rollback promise for devices already deleted by generation.
"""


def _editable(song, track):
    if track not in list(song.tracks) or track.is_frozen or not track.has_midi_input:
        raise RuntimeError("track unavailable, frozen or not MIDI")
    if song.record_mode or any(slot.is_recording for slot in track.clip_slots):
        raise RuntimeError("finish recording before rolling")


def _clip(track):
    return track.clip_slots[0].clip if track.clip_slots[0].has_clip else None


class OnTrackRoll:
    @staticmethod
    def device_containers(track):
        """Visit the track and rack chains, retaining the enclosing rack/chain identities."""
        queue = [(track, ())]
        seen = set()
        while queue:
            container, lineage = queue.pop(0)
            if id(container) in seen:
                continue
            seen.add(id(container))
            yield container, lineage
            if len(lineage) >= 16:
                continue
            for device in list(getattr(container, 'devices', ())):
                for chain in list(getattr(device, 'chains', ())):
                    queue.append((chain, lineage + (device, chain)))

    @classmethod
    def location(cls, track, generator):
        for container, lineage in cls.device_containers(track):
            for index, device in enumerate(list(getattr(container, 'devices', ()))):
                if device == generator:
                    return container, lineage, index
        return None

    def __init__(self, song, original, generator, token):
        _editable(song, original)
        self.song, self.original, self.generator, self.token = song, original, generator, token
        located = self.location(original, generator)
        if located is None:
            raise RuntimeError("generator unavailable")
        self.container, self.lineage, self.index = located
        if self.container is not original and not (
                getattr(self.container, 'has_midi_input', False)
                and getattr(self.container, 'has_audio_output', False)):
            raise RuntimeError("Use an Instrument Rack; MIDI Racks cannot hold sound")
        self.original_devices = list(self.container.devices)
        self.prefix = self.original_devices[:self.index + 1]
        self.original_clip = _clip(original)
        self.loaded_devices = None
        self.ready = False
        self.recovery = False
        self.source_busy = False
        self.pending_container = None
        self.pending_root = None

    def verify(self):
        _editable(self.song, self.original)
        located = self.location(self.original, self.generator)
        if located is None or located != (self.container, self.lineage, self.index):
            raise RuntimeError("generator or enclosing rack moved")
        current = list(self.container.devices)
        if current[:self.index + 1] != self.prefix:
            raise RuntimeError("generator or preceding devices changed")
        if _clip(self.original) != self.original_clip:
            raise RuntimeError("original clip changed while loading")

    def contains(self, container):
        if container is self.container:
            return True
        queue = [(d, 0) for d in list(self.container.devices)[self.index + 1:]]
        while queue:
            device, depth = queue.pop(0)
            if depth >= 8:
                continue
            for chain in list(getattr(device, "chains", ())):
                if chain == container:
                    return True
                queue.extend((child, depth + 1) for child in list(chain.devices))
        return False

    def capture_generated(self):
        self.verify()
        self.loaded_devices = list(self.container.devices)

    def discard(self):
        # Never automatically remove a still-initializing source. Removing its enclosing
        # generated Rack manually releases a failed pending load, without a script reload.
        if self.source_busy and self.pending_root in list(self.container.devices):
            return False
        return True

    def commit(self, replace_devices, replace_clip):
        self.verify()
        if not self.ready or self.source_busy:
            raise RuntimeError("content not verified")
        if replace_devices and len(list(self.container.devices)) <= self.index + 1:
            raise RuntimeError("replacement has no devices")
        self.ready = False
        return True
