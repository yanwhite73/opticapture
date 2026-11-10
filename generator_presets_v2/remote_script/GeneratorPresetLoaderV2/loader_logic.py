"""Pure decision logic for GeneratorPresetLoader — imports no Ableton modules, so it is unit-tested
outside Live. The two faults behind the 2026-09-01 crash + replay are both decisions that can be made
without touching Live, which is the point of isolating them here:

  1. Replay. A persisted ``Preset Request V2`` value must never be mistaken for a fresh command after a
     Set is reloaded. ``RequestGate`` records a per-generator baseline the first time it sees a value
     and only reports a command when a *later* value differs from that baseline. The parameter is not
     reset during restoration: an initialization write could itself look like a command to another
     observer, so first-observation baselining is the single authoritative replay guard.

  2. Crash. The preset load must never run while the device tree is still mutating — the crash was a
     use-after-free on a device replaced mid-roll. ``StabilityGate`` requires a run of quiet ticks
     (no ``devices_changed``) before a queued request may act, so the load only happens once the tree
     the roll built has settled.

Neither class calls Live; the Remote Script drives them with its own tick/listener callbacks.
"""

import math


class LatestRequestQueue:
    """FIFO across generators, latest-token-wins within one not-yet-started generator request."""

    def __init__(self):
        self._entries = []
        self._markers = set()

    def __bool__(self):
        return bool(self._entries)

    def __len__(self):
        return len(self._entries)

    def push(self, generator, marker, value):
        if marker in self._markers:
            for index, (_old_generator, queued_marker, _old_value) in enumerate(self._entries):
                if queued_marker == marker:
                    self._entries[index] = (generator, marker, value)
                    return "coalesced"
        self._markers.add(marker)
        self._entries.append((generator, marker, value))
        return "queued"

    def pop(self):
        generator, marker, value = self._entries.pop(0)
        self._markers.discard(marker)
        return generator, marker, value


class RequestGate:
    """Replay-safe command detector: only a value that CHANGES after the first observation counts."""

    def __init__(self):
        self._baseline = {}  # generator marker -> last value seen

    def observe(self, marker, value):
        """Record a value seen during a (re)scan — e.g. on connect or Set restore. Never a command."""
        self._baseline[marker] = value

    def is_command(self, marker, value):
        """True only when a prior baseline exists and the value changed from it.

        The first value for a generator is always treated as a baseline, so a restored request value
        can never trigger a load on its own.
        """
        previous = self._baseline.get(marker)
        self._baseline[marker] = value
        return previous is not None and value != previous

    def retain_only(self, markers_present):
        """Drop baselines for generators that have gone away, so a re-added device re-baselines."""
        self._baseline = {
            marker: value for marker, value in self._baseline.items() if marker in markers_present
        }

    def baseline_snapshot(self):
        return dict(self._baseline)


class StabilityGate:
    """Crash-safe timing: a queued load may run only after `quiet_ticks_required` churn-free ticks."""

    def __init__(self, quiet_ticks_required=3):
        if quiet_ticks_required < 1:
            quiet_ticks_required = 1
        self._required = quiet_ticks_required
        self._quiet = 0

    def note_change(self):
        """A device/track list changed — the tree is moving; restart the quiet count."""
        self._quiet = 0

    def tick(self):
        """One control-surface tick elapsed with no change since the last tick."""
        self._quiet += 1

    def is_stable(self):
        return self._quiet >= self._required

    def reset(self):
        self._quiet = 0


def numeric_snapshot(value, default):
    """Return a finite float snapshot, falling back when a Live value is absent or unreadable."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def preset_slot_count(device_count, preset_ratio, minimum=0):
    """Return the exact number of plan positions that should receive presets.

    The user-facing ratio describes one roll-wide allocation, not an independent coin flip for every
    device. Half-up rounding keeps the midpoint intuitive: 3 devices at 50% selects 2. ``minimum`` is
    the number of content-bearing source positions that cannot safely remain empty.
    """
    count = max(0, int(numeric_snapshot(device_count, 0.0)))
    floor = max(0, min(count, int(numeric_snapshot(minimum, 0.0))))
    ratio = max(0.0, min(100.0, numeric_snapshot(preset_ratio, 100.0)))
    target = int(math.floor((count * ratio / 100.0) + 0.5))
    return max(floor, min(count, target))


def select_preset_positions(device_count, preset_ratio, required_positions, sample):
    """Choose unique roll-plan positions once, before Browser loading starts.

    ``sample`` is injected (normally ``random.Random.sample``) so selection is deterministic in unit
    tests and no Live object leaks into this policy primitive. Invalid required positions are ignored;
    callers may safely derive the set from a partially validated plan.
    """
    count = max(0, int(numeric_snapshot(device_count, 0.0)))
    required = {
        int(position) for position in (required_positions or ())
        if isinstance(position, int) and 0 <= position < count
    }
    target = preset_slot_count(count, preset_ratio, len(required))
    optional = [position for position in range(count) if position not in required]
    chosen = sample(optional, target - len(required)) if target > len(required) else []
    return frozenset(required.union(chosen))


def resolve_candidate_pool(factory, source_matched, scope_only, factory_fallback):
    """Decide the final preset pool for one device from the two candidate lists.

    ``factory``        — the device's whole factory ``.adv`` pool (Browser).
    ``source_matched`` — items whose ``(device, name)`` is admitted by enabled personal sources
                         (drawn from factory and user pools, so tagged presets load wherever they live).

    Two user-chosen modes (design.md §5):

    * **include** (``scope_only`` False) — **prefer yours**: on a device where you have a matched
      preset, load one of *yours* (not factory); on a device where you have none, factory fills the
      gap. So every device gets a preset, and personal presets always win where they exist. (A
      plain union was tried first and rejected — it buried your presets in the much larger factory
      pool. A weighted "mostly yours + some factory" variant is a future option.)
    * **only** (``scope_only`` True) — restrict to your matched presets; a device with none falls back
      to a factory preset when ``factory_fallback`` is set, otherwise it is left to random params.

    So include ≡ only with an always-on factory fill. ``used_factory_fallback`` is reported only in
    ``only`` mode (include's gap-fill is normal, not a fallback event).

    Returns ``(pool, used_factory_fallback)``. Order-preserving, de-duplicated by object identity.
    """
    def _dedup(items):
        seen = set()
        out = []
        for item in items:
            key = id(item)
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out

    if not scope_only:  # include = prefer yours; factory only fills devices where you have none
        return (_dedup(source_matched), False) if source_matched else (_dedup(factory), False)
    if source_matched:
        return _dedup(source_matched), False
    if factory_fallback and factory:
        return _dedup(factory), True
    return [], False
