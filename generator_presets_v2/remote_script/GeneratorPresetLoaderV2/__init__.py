"""GeneratorPresetLoaderV2 — isolated companion for Optimystery.amxd.

The M4L device performs musical generation. This Control Surface publishes cached availability and
observes its versioned preparation/load/commit parameters. It loads allowlisted content on the
original track through Live's internal Browser interface. Max writes MIDI only after successful
loading and pad routing. Device replacement does not promise rollback of already deleted devices.

No socket, MIDI port, audio port, subprocess, or external Python process is used.
Explicit VST refresh writes its display cache inside the installed device folder.
Browser access is an internal Remote Script API and is intentionally guarded and logged.
"""

import os
import random
import time

import Live
from _Framework.ControlSurface import ControlSurface

from .browser_utils import (
    adv_folder_prune, all_user_presets, allowlisted_device_folder, available_supported_names,
    catalog_preset_index, children_of,
    user_catalog_preset_index,
    is_adv_preset, is_m4l_audio_effect,
    item_description, normalize_name, preset_name_key,
    matches_allowlisted_device, safe_attr,
    source_matched_candidates, walk, without_excluded,
)
from .device_catalog import (
    AVAILABILITY_CHANNELS, AVAILABILITY_CONTENT_PARAMETER, AVAILABILITY_REVISION_PARAMETER,
    CONTENT_REQUIRED_M4L_EFFECT_NAMES, DRUM_SOURCE_NAME, M4L_EFFECT_NAMES,
    M4L_INSTRUMENT_NAMES,
    NATIVE_EFFECT_NAMES, SAMPLE_HOST_NAMES,
    SYNTH_INSTRUMENT_NAMES, names_to_mask,
)
from .disk_index import build_index, installed_user_library
from .m4l_instruments import instrument_presets, choose_instrument_preset
from .roll_transaction import OnTrackRoll
from .preset_categories import category_selection, filter_native, filter_vst
from .roll_history import History
from .take_capture import TakeCapture
from .vst_presets import (
    RackPresets, choose_preset, verify_loaded_rack, write_pool_catalog, write_source_counts,
)
from .loader_logic import (
    LatestRequestQueue, RequestGate, StabilityGate, numeric_snapshot, resolve_candidate_pool,
    select_preset_positions,
)


REQUEST_PARAMETER = "Preset Request V2"
COMPLETE_PARAMETER = "Preset Complete V2"
PROTOCOL_VERSION = 4
RUNTIME_VERSION = "2.0.0-alpha.20-dev.51"
PROBE_PARAMETER = "Loader Probe V4"
REPLY_PARAMETER = "Loader Reply V4"
ACTIVE_PARAMETER = "Load Active V4"
RESULT_PARAMETER = "Load Result V4"
COMMIT_PARAMETER = "Commit Request V4"
COMMITTED_PARAMETER = "Commit Complete V4"
ERROR_PARAMETER = "Loader Error V4"
CLIP_PARAMETER = "Commit Clip V4"
DEVICES_PARAMETER = "Commit Devices V4"
KEEP_KIT_PARAMETER = "Keep Kit V4"
SAFETY_TITLE_PREFIX = "Safety "
PAD_STAGE_PARAMETER = "Pad Stage Request V5"
PAD_STAGE_READY_PARAMETER = "Pad Stage Ready V5"
PAD_STAGE_NAME = "Optimystery staging V5"
DRUM_PLAN_CAPABILITY = "Drum Plan Capability V6"
DRUM_PLAN_REQUEST = "Drum Plan Request V6"
DRUM_KIT_READY = "Drum Kit Ready V6"
DRUM_PLAN_READY = "Drum Plan Ready V6"
EFFECT_PLAN_CAPABILITY = "Effect Plan Capability V8"
SOURCE_PARAMETER = "Commit Source V9"
FX_PARAMETER = "Commit FX V9"
SCOPE_CAPABILITY = "Scope Capability V9"
M4L_FX_PLACEHOLDER_PREFIX = "Loading M4L FX: "
VST_REFRESH = "VST Refresh V7"
VST_AVAILABLE = "VST Available V7"
VST_READY = "VST Ready V7"
VST_ROLL = "VST Roll V7"
VST_SOURCE = "VST Source V7"
TRANSACTION_PARAMETERS = (PROBE_PARAMETER, REPLY_PARAMETER, ACTIVE_PARAMETER, RESULT_PARAMETER,
                          COMMIT_PARAMETER, COMMITTED_PARAMETER, ERROR_PARAMETER,
                          CLIP_PARAMETER, DEVICES_PARAMETER, KEEP_KIT_PARAMETER,
                          PAD_STAGE_PARAMETER, PAD_STAGE_READY_PARAMETER, DRUM_PLAN_CAPABILITY,
                          DRUM_PLAN_REQUEST, DRUM_KIT_READY, DRUM_PLAN_READY, EFFECT_PLAN_CAPABILITY)
USER_PARAMETER = "User Presets"
COLLECTION_PARAMETERS = (
    ("Collection Red", "1", "red"),
    ("Collection Orange", "2", "orange"),
    ("Collection Yellow", "3", "yellow"),
    ("Collection Green", "4", "green"),
    ("Collection Blue", "5", "blue"),
    ("Collection Purple", "6", "purple"),
    ("Collection Gray", "7", "gray"),
)
COLLECTION_NAMES_BY_ID = {color_id: label for _parameter, color_id, label in COLLECTION_PARAMETERS}
RATIO_PARAMETER = "Preset Ratio"  # exact roll-wide share of creative positions receiving presets
FALLBACK_PARAMETER = "Factory Fallback"  # if the source has no preset for a device, use a factory one
SCOPE_PARAMETER = "Preset Scope"  # 0 = include personal sources; 1 = only personal sources
M4L_PARAMETER = "M4L Devices"  # existing M4L audio-effects toggle; instruments have their own source
M4L_INSTRUMENT_PARAMETER = "M4L Instrument Source"
M4L_INSTRUMENT_ROLL_PARAMETER = "M4L Instrument Roll V2"
# Per-source Equal (0) / Presets (1) weighting; the companion owns these two choices.
M4L_WEIGHTING_PARAMETER = "M4L Instrument Weighting"
VST_WEIGHTING_PARAMETER = "VST Weighting"
M4L_FX_WEIGHTING_PARAMETER = "M4L FX Weighting"
# Effect type/family/Pool weighting is owned by the Max plan. The companion
# chooses only content within each explicitly planned effect.
GRANULAR_PARAMETER = "Granular"
# Max for Live device/folder keys (normalised). Your saved M4L presets live under "Max Audio Effect" /
# "Max Instrument"; the named ones are the first-party allowlist. Used to keep M4L presets out of the
# candidate pools when the M4L toggle is off (otherwise a saved M4L preset loads despite the toggle).
_M4L_KEYS = None  # built lazily (needs normalize_name)
# V2 uses a no-reset request baseline, tree-stability gate, and exact-token terminal acknowledgement.
HOT_SWAP_ENABLED = True
INSTRUMENT_NAMES = SYNTH_INSTRUMENT_NAMES + SAMPLE_HOST_NAMES
EFFECT_NAMES = NATIVE_EFFECT_NAMES + M4L_EFFECT_NAMES

# One parameter per catalog-derived mask channel, then content; revision is written last.
AVAILABILITY_PARAMETERS = tuple(
    (longname, key) for key, longname, _names in AVAILABILITY_CHANNELS
) + ((AVAILABILITY_CONTENT_PARAMETER, "content"),)


def create_instance(c_instance):
    return GeneratorPresetLoaderV2(c_instance)


def _live_id(obj):
    for name in ("_live_ptr", "_live_object_id"):
        try:
            value = getattr(obj, name)
            if value is not None:
                return (name, str(value))
        except Exception:
            pass
    return ("python", id(obj))


def _labels(device):
    values = []
    for name in ("class_display_name", "name", "class_name"):
        try:
            value = str(getattr(device, name))
            if value and value not in values:
                values.append(value)
        except Exception:
            pass
    return values


def _is_safety_utility(device):
    """True for the device's own safety Compressor/Limiter, which it titles "Safety …".

    Compressor is also a Pool effect, so type matching alone would load a preset over the safety
    one. Every plan-building site must check this before matching effect names.
    """
    return (str(safe_attr(device, "name", "") or "").startswith(SAFETY_TITLE_PREFIX)
            and _canonical_native_name(device, ("Compressor", "Limiter")) is not None)


def _canonical_native_name(device, allowed):
    allowed_by_key = {normalize_name(name): name for name in allowed}
    for label in _labels(device):
        key = normalize_name(label)
        if key in allowed_by_key:
            return allowed_by_key[key]
    return None


def _is_m4l_preset(item):
    """True if a Browser preset belongs to a Max for Live device (by folder/device name in its uri or
    name). Used to exclude M4L presets from the pools when the M4L toggle is off."""
    global _M4L_KEYS
    if _M4L_KEYS is None:
        _M4L_KEYS = frozenset(normalize_name(n) for n in (
            "Max Audio Effect", "Max Instrument", "Max MIDI Effect",
        ) + M4L_EFFECT_NAMES + M4L_INSTRUMENT_NAMES)
    blob = normalize_name(str(safe_attr(item, "uri", "")) + " " + str(safe_attr(item, "name", "")))
    return any(key in blob for key in _M4L_KEYS)


def _is_rack(device):
    try:
        return bool(getattr(device, "can_have_chains", False))
    except Exception:
        return False


def _is_drum_rack(device):
    """Identify the actual generated source slot, independent of which source buttons are enabled."""
    try:
        return bool(getattr(device, "can_have_drum_pads", False))
    except Exception:
        return False


def _first_chain(rack):
    try:
        chains = list(getattr(rack, "chains", ()))
        return chains[0] if chains else None
    except Exception:
        return None


def _missing_sample_file(device):
    """Inspect exposed Simpler samples, including inside kits. Opaque Sampler/plugin state is not
    certified by this function. Bounded traversal cannot loop forever on an unexpected host tree.
    """
    queue, seen = [(device, 0)], set()
    while queue:
        current, depth = queue.pop()
        marker = _live_id(current)
        if marker in seen:
            continue
        seen.add(marker)
        if len(seen) > 2048 or depth > 8:
            return "sample verification tree limit reached"
        if (_canonical_native_name(current, ("Simpler",)) == "Simpler"
                and not bool(safe_attr(current, "multi_sample_mode", False))):
            sample = safe_attr(current, "sample", None)
            path = str(safe_attr(sample, "file_path", "") or "")
            if (sample is None or not path or not os.path.isfile(path)
                    or not os.access(path, os.R_OK) or safe_attr(sample, "length", 0) <= 0):
                return "required sample missing or unreadable"
        if _is_rack(current):
            for chain in list(current.chains):
                queue.extend((child, depth + 1) for child in list(chain.devices))
    return None


class GeneratorPresetLoaderV2(ControlSurface):
    def __init__(self, c_instance):
        super().__init__(c_instance)
        self._alive = True
        self._request_bindings = []
        self._track_bindings = []
        self._tree_bindings = []
        self._tracks_listener_added = False
        self._rescan_scheduled = False
        self._pending = LatestRequestQueue()
        # Replay-safe request detection + crash-safe load timing live in pure, unit-tested helpers.
        self._request_gate = RequestGate()
        self._stability = StabilityGate()
        self._stability_waiting = False
        self._job = None
        self._owned_browser_target = None
        self._transactions = {}
        self._awaiting_commit = None
        self._probe_gate = RequestGate()
        self._commit_gate = RequestGate()
        self._vst_refresh_gate = RequestGate()
        self._vst_catalog = None  # ready/explicit refresh; no optional scan in the roll hot path
        self._vst_restore_pending = set()  # cached-list recovery after ready/preset recall
        self._scan_attempts = 0
        self._scan_retry_scheduled = False
        self._rng = random.SystemRandom()
        self._preset_index = None  # on-disk User/Collections index, built once on first use, cached
        self._availability_cache = None  # one Browser-derived capability snapshot per script session
        self._availability_attempted = False  # attempts are bounded; success remains cached
        self._availability_published = set()  # live ids of V2 instances that received that snapshot
        self._availability_refresh_pending = set()  # coalesced same-ID ready/recall requests
        self._your_pool_cache = {}  # (kind, user, collection IDs, m4l) -> all personal presets
        self._drum_kit_cache = None  # [BrowserItem] Drum Rack kits from browser.drums, built once
        self._m4l_audio_cache = {}  # enabled-name tuple -> matching BrowserItems
        self._m4l_instrument_cache = None
        self._factory_preset_cache = {}  # native preset candidates; personal filters remain per-job
        self._user_preset_cache = {}
        self._all_user_preset_cache = {}
        self._user_m4l_instrument_cache = {}

        if not HOT_SWAP_ENABLED:
            self.log_message(
                "GeneratorPresetLoaderV2: DISABLED after 2026-09-01 Browser/Hot-Swap crash; "
                "no listeners opened"
            )
            return

        self._history = None
        self._history_rolls = {}
        try:
            self._history = History(installed_user_library(), RUNTIME_VERSION, self.log_message)
            self.schedule_message(20, self._history_tick)
        except Exception as exc:
            self.log_message("GeneratorPresetLoaderV2: history unavailable: %s" % exc)
        self._add_song_listener()
        self._rescan()
        self._capture = None
        try:
            self._capture = TakeCapture(self, installed_user_library(), RUNTIME_VERSION,
                                        Live.Application.get_application())
            self.schedule_message(1, self._capture_tick)
        except Exception as exc:
            self.log_message("GeneratorPresetLoaderV2: capture unavailable: %s" % exc)
        self.log_message("GeneratorPresetLoaderV2: loaded %s; on-track rolls; no ports opened" % RUNTIME_VERSION)

    def _capture_tick(self):
        if not self._alive: return
        try:
            if self._capture: self._capture.tick()
        except Exception as exc:
            self.log_message("GeneratorPresetLoaderV2: capture tick failed: %s" % exc)
        self.schedule_message(2, self._capture_tick)

    def _history_tick(self):
        if not self._alive: return
        if not self._job and not self._transactions and self._history:
            self._history.tick()
        self.schedule_message(20, self._history_tick)

    def _history_context(self, generator, token, new=False):
        journal = getattr(self, "_history", None)
        if journal is None: return None
        key = (str(_live_id(generator)), token)
        records = self._history_rolls
        if new or key not in records:
            now = int(time.time()*1000)
            self._history_roll_serial = getattr(self,"_history_roll_serial",0)+1
            records[key] = dict(id=journal.session+":"+str(self._history_roll_serial),
                                token=token, started_ms=now, generator=key[0])
        return records[key]

    def _history_event(self, job, event, **data):
        journal = getattr(self, "_history", None)
        if journal is None: return
        try:
            context = self._history_context(job["generator"], job["request_value"], new=(event=="grant"))
            data.setdefault("position", job.get("position"))
            journal.emit(event, data, context)
        except Exception as exc: journal.warn(exc)

    def _history_choice(self, job, stage, items, weights, chosen):
        journal = getattr(self, "_history", None)
        if journal is None: return
        try:
            def identity(item):
                if isinstance(item, str): return dict(id=item,label=item)
                if isinstance(item, dict): return dict(id=item["path"],label=item.get("name",item["path"]))
                uri = str(safe_attr(item,"uri","") or "")
                name = str(safe_attr(item,"name","") or "")
                return dict(id=uri or "unresolved-name:"+name,label=name,identity_known=bool(uri))
            context = dict(types=sorted(job["sound_types"]) if job.get("sound_types") is not None else None,
                           scope_only=job.get("scope_only"), user=job.get("use_user"),
                           collections=job.get("collection_ids"), position=job.get("position"),
                           mode="presets" if (job.get("vst_proportional") if stage.startswith("vst") else job.get("m4l_proportional")) else "equal")
            journal.choice(stage,[identity(item) for item in items],weights,identity(chosen)["id"],context,
                           self._history_context(job["generator"],job["request_value"]))
        except Exception as exc: journal.warn(exc)

    def _get_preset_index(self):
        """Build (once) and cache the read-only on-disk preset index. A full library walk is not cheap,
        so it is built lazily and reused; re-selecting the Remote Script rebuilds it (picks up new
        Collection tags / saved presets). Fail-closed to empty — a filter must never crash a roll.
        """
        if self._preset_index is None:
            try:
                self._preset_index = build_index(
                    user_library=installed_user_library()
                )
                counts = self._preset_index.counts()
                collection_total = sum(counts["collections"].values())
                folders = self._preset_index.adv_folders
                self.log_message(
                    "GeneratorPresetLoaderV2: on-disk preset index built "
                    "(user=%d, collections=%d .adv memberships; browser pruning %s)" %
                    (counts["user"], collection_total,
                     "off (library unverified)" if folders is None else
                     "on (%d instrument, %d effect .adv folders)" % (
                         len(folders["Instruments"]), len(folders["Audio Effects"])))
                )
            except Exception as exc:
                self.log_message("GeneratorPresetLoaderV2: preset index build failed: %r" % (exc,))
                from .disk_index import PresetIndex
                self._preset_index = PresetIndex()
        return self._preset_index

    def _get_availability_snapshot(self):
        """Build the curated-catalog intersection once, then reuse it for every Generator instance."""
        if self._availability_cache is not None or self._availability_attempted:
            return self._availability_cache
        self._availability_attempted = True
        self._scan_attempts += 1
        try:
            browser = Live.Application.get_application().browser
            instruments_root = self._browser_attr(browser, "instruments")
            effects_root = self._browser_attr(browser, "audio_effects")
            if instruments_root is None or effects_root is None:
                raise RuntimeError("Live Browser instrument/effect roots are unavailable")
            self._prepare_preset_lists(browser)

            instrument_catalog = (
                SYNTH_INSTRUMENT_NAMES + SAMPLE_HOST_NAMES + (DRUM_SOURCE_NAME,)
            )
            available_instrument_nodes = available_supported_names(
                instruments_root, instrument_catalog
            )
            available_instruments = frozenset(SYNTH_INSTRUMENT_NAMES).intersection(
                available_instrument_nodes
            )
            available_sample_hosts = frozenset(SAMPLE_HOST_NAMES).intersection(
                available_instrument_nodes
            )
            available_native_effects = available_supported_names(
                effects_root, NATIVE_EFFECT_NAMES
            )
            drum_device_available = DRUM_SOURCE_NAME in available_instrument_nodes

            m4l_items = self._all_m4l_audio_effects(browser, M4L_EFFECT_NAMES)
            available_m4l = frozenset(
                name for name in M4L_EFFECT_NAMES
                if any(matches_allowlisted_device(
                    safe_attr(item, "uri", ""), safe_attr(item, "name", ""), (name,)
                ) for item in m4l_items)
            )
            content_mask = 0
            kits = self._all_drum_kits(browser)  # warm even an unavailable/empty source once
            if drum_device_available and bool(kits):
                content_mask |= 1
            user_root = self._browser_attr(browser, "user_library")
            for index, name in enumerate(SAMPLE_HOST_NAMES, start=1):
                if (name in available_sample_hosts
                        and bool(self._factory_presets("instrument", name)
                                 or self._user_preset_cache.get(("instrument", name)))):
                    content_mask |= 1 << index
            if self._all_m4l_instrument_presets(browser):
                content_mask |= 8

            available = available_instruments | available_native_effects | available_m4l
            cache = {key: names_to_mask(names, available)
                     for key, _longname, names in AVAILABILITY_CHANNELS}
            cache["content"] = content_mask
            self._availability_cache = cache
            self._write_source_counts(browser, kits)
            self.log_message(
                "GeneratorPresetLoaderV2: availability scan complete "
                "(instruments=%d/%d, native_fx=%d/%d, m4l_fx=%d/%d, content_mask=%d)" % (
                    len(available_instruments), len(SYNTH_INSTRUMENT_NAMES),
                    len(available_native_effects), len(NATIVE_EFFECT_NAMES),
                    len(available_m4l), len(M4L_EFFECT_NAMES), content_mask,
                )
            )
        except Exception as exc:
            self.log_message("GeneratorPresetLoaderV2: availability scan failed: %r" % (exc,))
            if self._alive and self._scan_attempts < 3 and not self._scan_retry_scheduled:
                self._scan_retry_scheduled = True
                self.schedule_message(20 * self._scan_attempts, self._retry_startup_scan)
            return None
        return self._availability_cache

    def _write_source_counts(self, browser, kits):
        """Publish the startup lists' preset counts for the device's source weighting."""
        def native(name):
            return (len(self._factory_presets("instrument", name))
                    + len(self._user_preset_cache.get(("instrument", name), ())))
        try:
            factory = self._all_m4l_instrument_presets(browser)
            user = self._user_m4l_instrument_cache or {}
            m4l = {name: len(factory.get(name, ())) + len(user.get(name, ()))
                   for name in M4L_INSTRUMENT_NAMES}
            instruments = {name: native(name) for name in SYNTH_INSTRUMENT_NAMES}
            hosts = {name: native(name) for name in SAMPLE_HOST_NAMES}
            effects = {name: len(self._factory_presets("audio_effect", name))
                       + len(self._user_preset_cache.get(("audio_effect", name), ()))
                       for name in NATIVE_EFFECT_NAMES}
            effects.update({name: max(1, sum(is_adv_preset(item) for item in items))
                            for name, items in self._m4l_effect_groups(M4L_EFFECT_NAMES).items()})
            write_source_counts(installed_user_library(), instruments, hosts, m4l, len(kits or ()), effects)
            self.log_message("GeneratorPresetLoaderV2: source preset counts written (%s; %s; %s; kits=%d)" % (
                ", ".join("%s=%d" % item for item in sorted(instruments.items())),
                ", ".join("%s=%d" % item for item in sorted(hosts.items())),
                ", ".join("%s=%d" % item for item in sorted(m4l.items())), len(kits or ())))
        except Exception as exc:
            # The device keeps its previous equal-source rule without this file.
            self.log_message("GeneratorPresetLoaderV2: source preset counts unavailable: %r" % (exc,))

    def _retry_startup_scan(self):
        self._scan_retry_scheduled = False
        if not self._alive or self._availability_cache is not None:
            return
        self._availability_attempted = False
        # Discard partial lists from the failed attempt. Successful lists are never scanned per roll.
        self._drum_kit_cache = self._m4l_instrument_cache = None
        self._m4l_audio_cache.clear()
        self._factory_preset_cache.clear()
        self._user_preset_cache.clear()
        if self._get_availability_snapshot() is not None:
            self._availability_published.clear()
            self._schedule_rescan()

    def _all_m4l_instrument_presets(self, browser):
        if self._m4l_instrument_cache is None:
            self._m4l_instrument_cache = self._without_excluded_pools(instrument_presets(
                self._browser_attr(browser, "instruments"), M4L_INSTRUMENT_NAMES))
            self.log_message("GeneratorPresetLoaderV2: M4L instrument presets: %s" %
                             ", ".join("%s=%d" % (name, len(items)) for name, items
                                       in self._m4l_instrument_cache.items()))
        return self._m4l_instrument_cache

    def _m4l_instrument_pools(self, job):
        browser = Live.Application.get_application().browser
        factory = self._all_m4l_instrument_presets(browser)
        if not job.get("source_active"):
            pools = {name: filter_native(items, job.get("sound_types")) for name, items in factory.items()}
            return {name: items for name, items in pools.items() if items}
        user = self._user_m4l_instrument_cache
        pools = {}
        for name in M4L_INSTRUMENT_NAMES:
            matched = source_matched_candidates(factory.get(name, []), user.get(name, []), name,
                job.get("use_user"), job.get("collection_ids"), job.get("preset_index"))
            matched = filter_native(matched, job.get("sound_types"))
            candidates, fallback = resolve_candidate_pool(filter_native(factory.get(name, []), job.get("sound_types")), matched,
                job.get("scope_only"), job.get("factory_fallback"))
            candidates = filter_native(candidates, job.get("sound_types"))
            if candidates:
                pools[name] = candidates
            if fallback:
                self.log_message("GeneratorPresetLoaderV2: factory fallback for M4L %s" % name)
        return pools

    def _m4l_family_counts(self):
        """Startup preset counts per M4L family (factory + User), the Presets weighting."""
        factory = self._m4l_instrument_cache or {}
        user = self._user_m4l_instrument_cache or {}
        return {name: len(factory.get(name, ())) + len(user.get(name, ())) for name in M4L_INSTRUMENT_NAMES}

    def _m4l_effect_groups(self, allowed_m4l):
        """Discovered M4L effect candidates per allowlisted device: its presets, else the device."""
        try:
            browser = Live.Application.get_application().browser
        except Exception:
            return {}
        groups = {}
        for item in self._all_m4l_audio_effects(browser, allowed_m4l) if allowed_m4l else []:
            uri, name = safe_attr(item, "uri", ""), safe_attr(item, "name", "")
            device = next((n for n in allowed_m4l if matches_allowlisted_device(uri, name, (n,))), None)
            if device is not None:
                groups.setdefault(device, []).append(item)
        for device, items in list(groups.items()):
            presets = [item for item in items if is_adv_preset(item)]
            groups[device] = presets or items
        return groups

    @staticmethod
    def _effect_plan_entry(index, device):
        """Decode only an owned generated slot; a named Max placeholder is never a native preset target."""
        title = str(safe_attr(device, "name", ""))
        if title.startswith(M4L_FX_PLACEHOLDER_PREFIX):
            name = title[len(M4L_FX_PLACEHOLDER_PREFIX):]
            if (name not in M4L_EFFECT_NAMES
                    or str(safe_attr(device, "class_name", "")) != "AudioEffectGroupDevice"
                    or list(safe_attr(device, "chains", ()))):
                raise RuntimeError("planned M4L effect placeholder changed")
            return (index, "m4l_effect", name)
        if str(safe_attr(device, "class_name", "")) == "AudioEffectGroupDevice":
            raise RuntimeError("planned M4L effect placeholder title missing")
        name = _canonical_native_name(device, NATIVE_EFFECT_NAMES)
        return (index, "audio_effect", name) if name is not None else None

    @staticmethod
    def _without_excluded_pools(pools):
        return {name: kept for name, kept in
                ((name, without_excluded(items)) for name, items in pools.items()) if kept}

    def _prepare_preset_lists(self, browser):
        """Build all Browser/disk-backed lists on load. Rolls only filter cached candidates."""
        started = time.monotonic()
        user_root = self._browser_attr(browser, "user_library")
        # The disk index goes first: it records which User Library preset folders hold any `.adv`,
        # so the Browser walks below can skip folders of third-party `.adg` racks entirely.
        adv_folders = self._get_preset_index().adv_folders
        for kind, names, root in (
                ("instrument", INSTRUMENT_NAMES, browser.instruments),
                ("audio_effect", EFFECT_NAMES, browser.audio_effects)):
            category = "Instruments" if kind == "instrument" else "Audio Effects"
            folders = adv_folders.get(category) if adv_folders is not None else None
            factory = catalog_preset_index(root, names)
            user = user_catalog_preset_index(user_root, kind, names, folders)
            for name in names:
                self._factory_preset_cache[(kind, name)] = without_excluded(factory.get(name, []))
                self._user_preset_cache[(kind, name)] = without_excluded(user.get(name, []))
            self._all_user_preset_cache[kind] = without_excluded(
                all_user_presets(user_root, kind, folders))
        self._user_m4l_instrument_cache = self._without_excluded_pools(instrument_presets(
            user_root, M4L_INSTRUMENT_NAMES, adv_folder_prune(
                adv_folders.get("Instruments") if adv_folders is not None else None,
                ("presets", "instruments"))))
        self.log_message("GeneratorPresetLoaderV2: preset lists ready (%d factory, %d user; %.1fms)" % (
            sum(map(len, self._factory_preset_cache.values())),
            sum(map(len, self._user_preset_cache.values())), (time.monotonic() - started) * 1000.0))

    def _factory_presets(self, kind, name):
        started = time.monotonic()
        result = self._factory_preset_cache.get((kind, name), [])
        if self._job is not None:
            self._job["lookup_ms"] = self._job.get("lookup_ms", 0.0) + (
                time.monotonic() - started) * 1000.0
        return result

    def _source_filtered_availability_snapshot(self, generator, snapshot):
        """Apply current source filters to startup lists; never discover Browser content on a roll."""
        result = dict(snapshot)
        use_user = self._parameter_enabled(generator, USER_PARAMETER)
        collections = self._selected_collection_ids(generator)
        if not (use_user or collections):
            return result
        only = self._parameter_enabled(generator, SCOPE_PARAMETER)
        fallback = self._parameter_enabled(generator, FALLBACK_PARAMETER)
        index = self._preset_index
        job = dict(source_active=True, use_user=use_user, collection_ids=collections,
                   scope_only=only, factory_fallback=fallback, preset_index=index)
        if self._drum_kit_cache is None or self._m4l_instrument_cache is None:
            raise RuntimeError("startup content lists are incomplete")
        if not self._drum_kit_pool(None, use_user, collections, index, only, fallback):
            result["content"] &= ~1
        for offset, name in enumerate(SAMPLE_HOST_NAMES, start=1):
            factory = self._factory_presets("instrument", name)
            matched = source_matched_candidates(factory,
                self._user_preset_cache.get(("instrument", name), []), name,
                use_user, collections, index)
            candidates, _ = resolve_candidate_pool(factory, matched, only, fallback)
            if not candidates:
                result["content"] &= ~(1 << offset)
        if not self._m4l_instrument_pools(job):
            result["content"] &= ~8
        if (only and not fallback and self._parameter_value(generator, RATIO_PARAMETER, 100) > 0
                and not self._your_preset_pool("instrument", use_user, collections, index, None, False)):
            for key, _longname, _names in AVAILABILITY_CHANNELS:
                if key.startswith("instruments_"):
                    result[key] = 0
        return result

    def _selected_sound_types(self, generator):
        return category_selection(lambda name, default: self._parameter_value(generator, name, default))

    def _native_preset_candidates(self, kind, name, job):
        categories = job.get("sound_types") if kind == "instrument" else None
        factory = filter_native(self._factory_presets(kind, name), categories)
        use_user, collections = job.get("use_user"), job.get("collection_ids") or ()
        if not (use_user or collections):
            return factory, False, False
        matched = filter_native(source_matched_candidates(
            self._factory_presets(kind, name), self._user_preset_cache.get((kind, name), []),
            name, use_user, collections, job.get("preset_index")), categories)
        if (job.get("scope_only") and not matched and kind == "instrument"
                and name not in SAMPLE_HOST_NAMES):
            # Preserve the existing personal-preset cross-device fill, within types.
            candidates = filter_native(self._your_preset_pool(kind, use_user, collections,
                job.get("preset_index"), None, False), categories)
            if candidates:
                return candidates, False, True
            return (factory, True, False) if job.get("factory_fallback") else ([], False, False)
        candidates, fallback = resolve_candidate_pool(factory, matched,
            job.get("scope_only"), job.get("factory_fallback"))
        return candidates, fallback, False

    def _filtered_availability_snapshot(self, generator, snapshot, categories=None):
        result = self._source_filtered_availability_snapshot(generator, snapshot)
        if categories is None:
            return result  # default All keeps the original inventory and RNG
        if self._m4l_instrument_cache is None:
            raise RuntimeError("startup content lists are incomplete")
        use_user, collections = self._parameter_enabled(generator, USER_PARAMETER), self._selected_collection_ids(generator)
        job = dict(sound_types=categories, source_active=bool(use_user or collections),
                   use_user=use_user, collection_ids=collections, preset_index=self._preset_index,
                   scope_only=self._parameter_enabled(generator, SCOPE_PARAMETER),
                   factory_fallback=self._parameter_enabled(generator, FALLBACK_PARAMETER))
        for key, _longname, names in AVAILABILITY_CHANNELS:
            if key.startswith("instruments_"):
                eligible = [name for name in names if self._native_preset_candidates("instrument", name, job)[0]]
                result[key] &= names_to_mask(names, eligible)
        for offset, name in enumerate(SAMPLE_HOST_NAMES, start=1):
            if not self._native_preset_candidates("instrument", name, job)[0]:
                result["content"] &= ~(1 << offset)
        if not self._m4l_instrument_pools(job):
            result["content"] &= ~8
        return result

    def _publish_availability(self, generator, marker, snapshot=None):
        """Write masks first and a changed revision last so Max applies one atomic snapshot."""
        if not self._alive or marker in self._availability_published:
            return self._alive and marker in self._availability_published
        snapshot = self._get_availability_snapshot() if snapshot is None else snapshot
        if snapshot is None:
            return False
        parameters = {
            key: self._find_parameter(generator, parameter_name)
            for parameter_name, key in AVAILABILITY_PARAMETERS
        }
        revision_parameter = self._find_parameter(generator, AVAILABILITY_REVISION_PARAMETER)
        missing = [key for key, parameter in parameters.items() if parameter is None]
        if revision_parameter is None:
            missing.append("revision")
        if missing:
            self.log_message(
                "GeneratorPresetLoaderV2: fresh Optimystery instance required; "
                "availability bridge missing %s" % ",".join(missing)
            )
            return False
        try:
            for _parameter_name, key in AVAILABILITY_PARAMETERS:
                parameters[key].value = float(snapshot[key])
            # Zero requests a fresh snapshot after ready/recall; positive values commit it.
            revision = (int(round(float(revision_parameter.value))) % 127) + 1
            revision_parameter.value = float(revision)
            self._availability_published.add(marker)
            self.log_message(
                "GeneratorPresetLoaderV2: availability revision %d published" % revision
            )
            return True
        except Exception as exc:
            self.log_message("GeneratorPresetLoaderV2: availability publish failed: %r" % (exc,))
            return False

    def _request_availability_refresh(self, generator, marker):
        """Answer a device reset from the cached inventory, outside parameter callbacks."""
        if not self._alive or marker in self._availability_refresh_pending:
            return
        self._availability_refresh_pending.add(marker)

        def publish():
            if not self._alive or self._locate_device(generator) is None:
                self._availability_refresh_pending.discard(marker)
                return
            # A roll owns a filtered snapshot until its transaction and load have ended.
            if (marker in self._transactions
                    or (self._job is not None and self._job.get("marker") == marker)):
                self.schedule_message(1, publish)
                return
            self._availability_refresh_pending.discard(marker)
            self._availability_published.discard(marker)
            if self._publish_availability(generator, marker):
                self._restore_vst_availability(generator, marker)

        self.schedule_message(1, publish)

    def _your_preset_pool(self, kind, use_user, collection_ids, index, browser, allow_m4l):
        """All of YOUR presets of one ``kind`` (across every device), as loadable BrowserItems. This is
        what fills ``only`` mode on a slot whose generated device you have no preset for: the slot is
        replaced with one of your presets (so the chain is only your preset-devices; with few, one can
        repeat). Collection membership is read from disk; loadable objects still come from Browser.
        Native instrument slots always pass allow_m4l=False; the dedicated M4L source has its own
        family-scoped pool. Effect slots follow the M4L effects toggle. Cached per (kind, sources,
        m4l) for the session.
        """
        collection_ids = tuple(collection_ids or ())
        key = (kind, bool(use_user), collection_ids, bool(allow_m4l))
        if key in self._your_pool_cache:
            return self._your_pool_cache[key]
        items = []
        seen = set()

        def add(item):
            if not allow_m4l and _is_m4l_preset(item):
                return  # this slot does not permit M4L presets
            marker = str(safe_attr(item, "uri", "") or "") or str(id(item))
            if marker not in seen:
                seen.add(marker)
                items.append(item)

        try:
            names = SYNTH_INSTRUMENT_NAMES if kind == "instrument" else EFFECT_NAMES
            if use_user:
                for item in self._all_user_preset_cache.get(kind, []):
                    add(item)
            if collection_ids and index is not None:
                for name in names:
                    device_key = normalize_name(name)
                    candidates = (self._factory_presets(kind, name)
                                  + self._user_preset_cache.get((kind, name), []))
                    for item in candidates:
                        if index.matches(device_key, preset_name_key(safe_attr(item, "name", "")),
                                         use_user=False, collection_ids=collection_ids):
                            add(item)
        except Exception as exc:
            self.log_message("GeneratorPresetLoaderV2: your-preset pool (%s) failed: %r" % (kind, exc))
        self._your_pool_cache[key] = items
        self.log_message("GeneratorPresetLoaderV2: your %s preset pool = %d item(s)" % (kind, len(items)))
        return items

    _SAMPLE_EXTS = (".wav", ".aif", ".aiff", ".aifc", ".flac", ".mp3", ".ogg", ".m4a")

    def _drum_kit_pool(self, browser, use_user, collection_ids, index,
                       scope_only, factory_fallback):
        """Resolve Drum Rack kits through the same include/only/fallback policy as device presets."""
        all_kits = self._all_drum_kits(browser)
        if not (use_user or collection_ids):
            return all_kits
        wanted = index.kit_names(use_user, collection_ids) if index is not None else set()
        personal = [kit for kit in all_kits
                    if preset_name_key(safe_attr(kit, "name", "")) in wanted]
        kits, used_fallback = resolve_candidate_pool(
            all_kits, personal, scope_only, factory_fallback
        )
        if used_fallback:
            self.log_message("GeneratorPresetLoaderV2: factory fallback for Drum Rack kit")
        return kits

    def _all_drum_kits(self, browser):
        """Full Drum Rack **kits** from the Browser's Drums category (built once, cached). A kit is a
        `.adg` (a whole Drum Rack with pads) — NOT a raw one-shot sample. Live's Drums category holds
        both, and single samples were loading as single-sample devices, so raw audio leaves are excluded
        here. Logs a sample of what it found so the structure is visible."""
        if self._drum_kit_cache is not None:
            return self._drum_kit_cache
        full_kits, single_drums, samples = [], [], []
        seen = set()
        try:
            root = self._browser_attr(browser, "drums")
            if root is not None:
                for item, _depth in walk(root):
                    if not bool(safe_attr(item, "is_loadable", False)):
                        continue
                    if bool(safe_attr(item, "is_device", False)):
                        continue  # bare Drum Rack device (no content) — skip
                    name = str(safe_attr(item, "name", ""))
                    uri = str(safe_attr(item, "uri", ""))
                    marker = uri or name or str(id(item))
                    if marker in seen:
                        continue
                    seen.add(marker)
                    low = name.lower()
                    if low.endswith(self._SAMPLE_EXTS) or uri.lower().endswith(self._SAMPLE_EXTS):
                        samples.append(item)     # raw one-shot
                    elif "kit" in low:
                        full_kits.append(item)   # full multi-pad kit, e.g. "707 Kit.adg"
                    else:
                        single_drums.append(item)  # single-drum .adg (Bell, Cowbell, Splash) — not a kit
        except Exception as exc:
            self.log_message("GeneratorPresetLoaderV2: drum kit pool failed: %r" % (exc,))
        pool = full_kits if full_kits else single_drums  # prefer real kits; fall back only if none
        pool = without_excluded(pool)
        self._drum_kit_cache = pool
        example = ", ".join(str(safe_attr(it, "name", "?")) for it in pool[:8])
        self.log_message(
            "GeneratorPresetLoaderV2: drum kit pool = %d full kit(s) (%d single-drum .adg, %d raw samples "
            "skipped). e.g.: %s" % (len(full_kits), len(single_drums), len(samples), example or "(none)"))
        return pool

    def _all_m4l_audio_effects(self, browser, allowed_names):
        """Cache supported audio effects from native and dedicated Max Browser branches."""
        key = tuple(sorted(normalize_name(name) for name in allowed_names))
        if key in self._m4l_audio_cache:
            return self._m4l_audio_cache[key]
        if not key:
            return []
        full_key = tuple(sorted(normalize_name(name) for name in M4L_EFFECT_NAMES))
        if key != full_key:
            # The full catalog is discovered on load. Include changes filter that list without
            # traversing the Browser again during a roll.
            self._m4l_audio_cache[key] = [item for item in self._m4l_audio_cache.get(full_key, [])
                if matches_allowlisted_device(safe_attr(item, "uri", ""),
                                              safe_attr(item, "name", ""), allowed_names)]
            return self._m4l_audio_cache[key]
        found = []
        seen = set()
        roots = [("audio_effects", self._browser_attr(browser, "audio_effects"), False)]
        # Ableton's installed Push browser model queries this separate branch for
        # Max audio effects. Its siblings contain instruments and MIDI effects.
        max_root = self._browser_attr(browser, "max_for_live")
        max_children = children_of(max_root)
        audio_branches = [item for item in max_children
                          if normalize_name(safe_attr(item, "name", "")) == "maxaudioeffect"]
        if not audio_branches:
            self.log_message("GeneratorPresetLoaderV2: M4L discovery: max_for_live/Max Audio Effect "
                             "missing; children=%r" % [str(safe_attr(item, "name", ""))[:100]
                                                       for item in max_children[:6]])
        roots.extend(("max_for_live/Max Audio Effect", item, True) for item in audio_branches)
        for label, root, audio_context in roots:
            visited, loadable, accepted = 0, 0, 0
            marker_rejections = []
            try:
                for item, _depth in (walk(root) if root is not None else ()):
                    visited += 1
                    if not bool(safe_attr(item, "is_loadable", False)):
                        continue
                    loadable += 1
                    name = str(safe_attr(item, "name", ""))
                    uri = str(safe_attr(item, "uri", ""))
                    if not matches_allowlisted_device(uri, name, allowed_names):
                        continue
                    # The Max Audio Effect branch supplies type context even when
                    # a leaf's name/opaque URI has no .amxd or Max marker.
                    if (not audio_context and not is_m4l_audio_effect(uri, name)
                            and not allowlisted_device_folder(uri, allowed_names)):
                        if len(marker_rejections) < 3:
                            marker_rejections.append((name[:100], uri[:200]))
                        continue
                    # A bare Convolution device has no guaranteed impulse response. Only its `.adv`
                    # leaves carry the content that makes this a valid generated effect.
                    if (matches_allowlisted_device(
                            uri, name, CONTENT_REQUIRED_M4L_EFFECT_NAMES)
                            and not is_adv_preset(item)):
                        continue
                    marker = uri or name or str(id(item))
                    if marker in seen:
                        continue
                    seen.add(marker)
                    found.append(item)
                    accepted += 1
            except Exception as exc:
                self.log_message("GeneratorPresetLoaderV2: M4L audio-effect scan failed in %s: %r" %
                                 (label, exc))
            self.log_message("GeneratorPresetLoaderV2: M4L discovery: root=%s present=%s "
                             "visited=%d loadable=%d accepted=%d" %
                             (label, root is not None, visited, loadable, accepted))
            if marker_rejections:
                self.log_message("GeneratorPresetLoaderV2: M4L discovery: missing M4L marker in %s: %r" %
                                 (label, marker_rejections))
        self._m4l_audio_cache[key] = found
        example = ", ".join(str(safe_attr(it, "name", "?")) for it in found[:8])
        self.log_message(
            "GeneratorPresetLoaderV2: discovered supported M4L audio effects = %d. e.g.: %s" %
            (len(found), example or "(none)"))
        return found

    def disconnect(self):
        self._alive = False
        scan = getattr(self, "_vst_scan", None)
        if scan is not None:
            scan.cancel()
            self._vst_scan = None
        if self._job is not None:
            self._job["failed"] += 1
            self._finish_job("Remote Script disconnected")
        for marker in list(self._transactions):
            self._cancel_transaction(marker)
        self._clear_request_bindings()
        self._clear_track_bindings()
        self._remove_song_listener()
        super().disconnect()

    # -- listener lifecycle -------------------------------------------------

    def _add_song_listener(self):
        try:
            song = self.song()
            if not song.tracks_has_listener(self._on_tracks_changed):
                song.add_tracks_listener(self._on_tracks_changed)
                self._tracks_listener_added = True
        except Exception as exc:
            self.log_message("GeneratorPresetLoaderV2: tracks listener failed: %r" % (exc,))

    def _remove_song_listener(self):
        try:
            if self._tracks_listener_added and self.song().tracks_has_listener(self._on_tracks_changed):
                self.song().remove_tracks_listener(self._on_tracks_changed)
        except Exception:
            pass
        self._tracks_listener_added = False

    def _on_tracks_changed(self):
        self._stability.note_change()  # the tree is moving; hold any queued load until it settles
        self._schedule_rescan()

    def _on_devices_changed(self):
        self._stability.note_change()
        self._schedule_rescan()

    def _schedule_rescan(self):
        if not self._alive or self._rescan_scheduled:
            return
        self._rescan_scheduled = True
        self.schedule_message(1, self._rescan)

    def _discovery_containers(self):
        containers = []
        for track in list(self.song().tracks):
            for container, _lineage in OnTrackRoll.device_containers(track):
                containers.append(container)
                for device in list(getattr(container, "devices", ())):
                    # Empty racks need a chains listener too. Max parameters can become
                    # available after the device itself appears during Set/rack recall.
                    events = []
                    if bool(getattr(device, "can_have_chains", False)):
                        events.append("chains")
                    if str(getattr(device, "class_name", "")) == "MxDeviceMidiEffect":
                        events.append("parameters")
                    for event in events:
                        try:
                            has = getattr(device, event + "_has_listener")
                            if not has(self._on_devices_changed):
                                getattr(device, "add_" + event + "_listener")(self._on_devices_changed)
                                self._tree_bindings.append((device, event))
                        except Exception as exc:
                            self.log_message("GeneratorPresetLoaderV2: %s listener failed: %r" % (event, exc))
        return containers

    def _rescan(self):
        self._rescan_scheduled = False
        if not self._alive:
            return
        self._clear_request_bindings()
        self._clear_track_bindings()
        found = 0
        request_values = []
        seen_request_ids = set()
        for track in self._discovery_containers():
            try:
                if not track.devices_has_listener(self._on_devices_changed):
                    track.add_devices_listener(self._on_devices_changed)
                    self._track_bindings.append(track)
            except Exception:
                pass
            for device in list(getattr(track, "devices", ())):
                # Immutable structural gate: old devices cannot enter the new staging protocol.
                if any(self._find_parameter(device, name) is None for name in TRANSACTION_PARAMETERS):
                    continue
                generator_marker = None
                for parameter in list(getattr(device, "parameters", ())):
                    try:
                        if str(parameter.name) != REQUEST_PARAMETER:
                            continue
                    except Exception:
                        continue
                    callback = self._make_request_callback(device, parameter)
                    try:
                        parameter.add_value_listener(callback)
                        self._request_bindings.append((parameter, callback))
                        found += 1
                        value = int(round(float(parameter.value)))
                        marker = _live_id(device)
                        generator_marker = marker
                        seen_request_ids.add(marker)
                        request_values.append(value)
                        # Device insertion/removal can force a rescan in the same Max event that
                        # changes Preset Request. The gate recovers a callback missed while listeners
                        # were being rebuilt, and — because a generator's first-seen value is only a
                        # baseline — a restored Set never replays as a command.
                        if self._request_gate.is_command(marker, value):
                            self._queue_request(device, value)
                    except Exception as exc:
                        self.log_message("GeneratorPresetLoaderV2: request listener failed: %r" % (exc,))
                if generator_marker is not None:
                    self._bind_transaction_parameters(device, generator_marker)
                if (generator_marker is not None
                        and generator_marker not in self._availability_published):
                    def publish(target=device, target_marker=generator_marker):
                        if self._publish_availability(target, target_marker):
                            self._restore_vst_availability(target, target_marker)
                    # Initial publication also supports older devices. New devices explicitly
                    # request again if ready/recall happens after this publication. Script reload
                    # leaves an open device intact, so cannot depend on another device_ready.
                    self.schedule_message(2, publish)
        self._request_gate.retain_only(seen_request_ids)
        self._probe_gate.retain_only(seen_request_ids)
        self._commit_gate.retain_only(seen_request_ids)
        for marker in list(self._transactions):
            if marker not in seen_request_ids:
                self._cancel_transaction(marker)
        self._availability_published.intersection_update(seen_request_ids)
        self.log_message(
            "GeneratorPresetLoaderV2: watching %d generator(s); request values=%r" %
            (found, request_values)
        )

    def _set_parameter(self, generator, name, value):
        parameter = self._find_parameter(generator, name)
        if parameter is None:
            raise RuntimeError("missing transaction parameter: " + name)
        parameter.value = float(value)

    def _active(self, generator, token):
        return (self._alive and token > 0
                and self._parameter_value(generator, ACTIVE_PARAMETER, 0) == token)

    def _bind_transaction_parameters(self, generator, marker):
        revision = self._find_parameter(generator, AVAILABILITY_REVISION_PARAMETER)
        if revision is not None:
            def availability_refresh(p=revision, device=generator, key=marker):
                if self._alive and float(p.value) == 0:
                    self._request_availability_refresh(device, key)
            revision.add_value_listener(availability_refresh)
            self._request_bindings.append((revision, availability_refresh))
            availability_refresh()  # recover a request made before listeners were attached
        refresh = self._find_parameter(generator, VST_REFRESH)
        if refresh is not None:
            if not hasattr(self, "_vst_refresh_gate"):
                self._vst_refresh_gate = RequestGate()
            def vst_refresh(p=refresh, device=generator, key=marker):
                if self._alive and self._vst_refresh_gate.is_command(key, int(round(float(p.value)))):
                    self.schedule_message(1, lambda: self._refresh_vst(device))
            refresh.add_value_listener(vst_refresh)
            self._request_bindings.append((refresh, vst_refresh))
            vst_refresh()
        for name, gate, handler in ((PROBE_PARAMETER, self._probe_gate, self._answer_probe),
                                    (COMMIT_PARAMETER, self._commit_gate, self._commit_transaction)):
            parameter = self._find_parameter(generator, name)
            def callback(p=parameter, g=gate, action=handler, device=generator, key=marker):
                if not self._alive:
                    return
                value = int(round(float(p.value)))
                if g.is_command(key, value) and value > 0:
                    self.schedule_message(1, lambda: action(device, value))
            parameter.add_value_listener(callback)
            self._request_bindings.append((parameter, callback))
            callback()  # first sight is a baseline; later rescans recover a missed callback
        active = self._find_parameter(generator, ACTIVE_PARAMETER)
        def canceled():
            if self._parameter_value(generator, ACTIVE_PARAMETER, 0) == 0:
                self.schedule_message(1, lambda: self._cancel_transaction(marker))
        active.add_value_listener(canceled)
        self._request_bindings.append((active, canceled))
        if marker in self._transactions:
            canceled()

    def _restore_vst_availability(self, generator, marker):
        """Recover VST list/status after recall, reusing this session's completed catalog."""
        if (not self._alive or marker in self._vst_restore_pending
                or self._find_parameter(generator, VST_AVAILABLE) is None):
            return
        self._vst_restore_pending.add(marker)
        cleared = False

        def restore():
            nonlocal cleared
            try:
                if not self._alive or self._locate_device(generator) is None:
                    self._vst_restore_pending.discard(marker)
                    return
                # Keep native readiness independent; VST recovery waits for any active load/listing.
                if (self._job is not None or self._transactions
                        or getattr(self, "_vst_scan", None) is not None):
                    self.schedule_message(1, restore)
                    return
                # A saved positive count is not a current response. Force a changed completion
                # even if the rebuilt list has exactly the same count as the saved parameter.
                catalog = getattr(self, "_vst_catalog", None)
                if not cleared:
                    self._set_parameter(generator, VST_AVAILABLE, 0)
                    cleared = True
                    if catalog is not None:
                        # Give the host a tick to deliver zero before an identical cached count.
                        # The next callback checks presence and active work again before writing.
                        self.schedule_message(1, restore)
                        return
                self._vst_restore_pending.discard(marker)
                if catalog is not None:
                    code = (len(catalog.candidates()) or -5) if catalog.entries else -8
                    self._finish_vst_refresh(generator, catalog.root, code)
                    self.log_message("GeneratorPresetLoaderV2: cached VST list restored (%d presets)" % len(catalog.entries))
                elif (self._parameter_enabled(generator, VST_SOURCE)
                      or self._parameter_value(generator, "Pool View", 0) == 1):
                    self._refresh_vst(generator)
            except Exception as exc:
                self._vst_restore_pending.discard(marker)
                self.log_message("GeneratorPresetLoaderV2: VST recall recovery failed: %r" % (exc,))
                try:
                    self._set_parameter(generator, VST_AVAILABLE, -6)
                except Exception:
                    pass  # device may have disappeared while queued

        self.schedule_message(1, restore)

    def _refresh_vst(self, generator):
        if self._alive and getattr(self, "_vst_scan", None) is not None:
            self._set_parameter(generator, VST_AVAILABLE, self._vst_status)
            return
        if not self._alive or self._job is not None or self._transactions:
            if self._alive:
                self._set_parameter(generator, VST_AVAILABLE, -9)
            return  # refresh never races a load/commit or clears its quarantine
        old = getattr(self, "_vst_catalog", None)
        self._vst_blocked = getattr(self, "_vst_blocked", set()) | (old.blocked if old else set())
        self._vst_status = -1
        self._set_parameter(generator, VST_AVAILABLE, -1)
        root, code = installed_user_library(), -4
        try:
            catalog = RackPresets(root, Live.Application.get_application().browser, self._vst_blocked)
            self._vst_scan = catalog
            self.schedule_message(1, lambda: self._continue_vst_refresh(generator, catalog))
            return
        except Exception as exc:
            code = getattr(exc, "code", -4)
            self.log_message("GeneratorPresetLoaderV2: VST remains unavailable: %s" % exc)
        self._finish_vst_refresh(generator, root, code)

    def _continue_vst_refresh(self, generator, catalog):
        if not self._alive or getattr(self, "_vst_scan", None) is not catalog:
            catalog.cancel()
            return
        try:
            present = self._find_parameter(generator, VST_AVAILABLE) is not None
        except Exception:
            present = False  # Live invalidates the MaxDevice wrapper on removal
        if not present:
            catalog.cancel()
            self._vst_scan = None
            self._vst_status = 0
            self.log_message("GeneratorPresetLoaderV2: VST refresh cancelled; requesting device removed")
            return
        if self._job is not None or self._transactions:
            self.schedule_message(1, lambda: self._continue_vst_refresh(generator, catalog))
            return
        code, published = -4, False
        try:
            tick = time.monotonic()
            complete = catalog.advance()
            catalog.callback_ms = max(catalog.callback_ms, (time.monotonic() - tick) * 1000)
            if not complete:
                self._vst_status = -1000 - catalog.processed
                self._set_parameter(generator, VST_AVAILABLE, self._vst_status)
                self.schedule_message(1, lambda: self._continue_vst_refresh(generator, catalog))
                return
            enabled_count = len(catalog.candidates())
            code = (enabled_count or -5) if catalog.entries else -8
            # Publish only a complete readable generation. A failed refresh must
            # retain the previous catalog and the user's existing choices.
            write_pool_catalog(catalog.root, catalog, code)
            self._vst_catalog = catalog
            published = True
            self.log_message("GeneratorPresetLoaderV2: Rack preset listing: %d/%d files in %d instrument "
                             "folders, %d enabled; %.3fs, max_callback=%.3fms; skipped=%r; examples=%r" %
                             (len(catalog.entries), catalog.total,
                              len({entry["plugin_key"] for entry in catalog.entries}), enabled_count,
                              time.monotonic() - catalog.started, catalog.callback_ms, catalog.skipped,
                              catalog.skipped_examples))
        except Exception as exc:
            catalog.cancel()
            code = getattr(exc, "code", -4)
            self.log_message("GeneratorPresetLoaderV2: VST index unavailable: %s" % exc)
        self._vst_scan = None
        self._finish_vst_refresh(generator, catalog.root, code, published=published)

    def _finish_vst_refresh(self, generator, root, code, published=False):
        try:
            if not published:
                write_pool_catalog(root, getattr(self, "_vst_catalog", None), code)
        except Exception as exc:
            code = -6
            self.log_message("GeneratorPresetLoaderV2: VST Pool display unavailable: %s" % exc)
        self._vst_status = code
        self._set_parameter(generator, VST_AVAILABLE, code)

    def _answer_probe(self, generator, token):
        if not self._active(generator, token):
            return
        marker = _live_id(generator)
        try:
            # Preparation changes Live's device tree too. Serialize from probe through commit,
            # including Max-side native insertion and pad routing, not just Browser calls.
            if (any(key != marker for key in self._transactions)
                    or (self._job is not None and self._job.get("marker") != marker)):
                self._set_parameter(generator, ERROR_PARAMETER, 8)
                self._set_parameter(generator, REPLY_PARAMETER, -token)
                return
            if marker in self._transactions and not self._cancel_transaction(marker):
                self._set_parameter(generator, ERROR_PARAMETER, 7)
                self._set_parameter(generator, REPLY_PARAMETER, -token)
                return
            snapshot = self._get_availability_snapshot()
            if snapshot is None:
                raise RuntimeError("Browser index unavailable; startup recovery pending or exhausted")
            located = self._locate_device(generator)
            if located is None:
                raise RuntimeError("generator unavailable")
            original, _index = located
            self._availability_published.discard(marker)
            legacy = self._parameter_enabled(generator, DEVICES_PARAMETER)
            roll_source = bool(self._parameter_value(generator, SOURCE_PARAMETER, int(legacy)))
            roll_fx = bool(self._parameter_value(generator, FX_PARAMETER, int(legacy)))
            categories = self._selected_sound_types(generator) if roll_source else None
            filtered = self._filtered_availability_snapshot(generator, snapshot, categories)
            if not self._publish_availability(generator, marker, filtered):
                raise RuntimeError("availability publication failed")
            self._transactions[marker] = OnTrackRoll(self.song(), original, generator, token)
            self._transactions[marker].sound_types = categories
            self._transactions[marker].roll_source = roll_source
            self._transactions[marker].roll_fx = roll_fx
            self._transactions[marker].roll_clip = self._parameter_enabled(generator, CLIP_PARAMETER)
            self._history_event(dict(generator=generator,request_value=token), "grant",
                source=roll_source, fx=roll_fx, clip=self._transactions[marker].roll_clip,
                types=sorted(categories) if categories is not None else None)
            self.log_message("GeneratorPresetLoaderV2: grant %d source=%s fx=%s types=%s type_solo=%s" %
                             (token, roll_source, roll_fx, sorted(categories) if categories is not None else "all",
                              self._parameter_value(generator, "Pool Type Solo", 0)))
            self._set_parameter(generator, ERROR_PARAMETER, 0)
            self._set_parameter(generator, PAD_STAGE_READY_PARAMETER, token)
            self._set_parameter(generator, DRUM_PLAN_CAPABILITY, token)
            self._set_parameter(generator, EFFECT_PLAN_CAPABILITY, token)
            if self._find_parameter(generator, SCOPE_CAPABILITY) is not None:
                self._set_parameter(generator, SCOPE_CAPABILITY, token)
            if self._find_parameter(generator, VST_READY) is not None:
                catalog = getattr(self, "_vst_catalog", None)
                try:
                    available = (-1 if getattr(self, "_vst_scan", None) is not None else
                                 (len(filter_vst(catalog.candidates(), categories)) or -5) if catalog else getattr(self, "_vst_status", 0))
                except Exception as exc:
                    # An optional pool problem must not reject a native roll's probe.
                    available = -6
                    self.log_message("GeneratorPresetLoaderV2: VST Pool unavailable: %s" % exc)
                self._set_parameter(generator, VST_AVAILABLE, available)
                self._set_parameter(generator, VST_READY, token if available > 0 else 0)
            self._set_parameter(generator, REPLY_PARAMETER, token)
        except Exception as exc:
            self._history_event(dict(generator=generator,request_value=token),"terminal",outcome="refused",detail=str(exc))
            self.log_message("GeneratorPresetLoaderV2: preparation failed: %s" % exc)
            self._set_parameter(generator, ERROR_PARAMETER, 1)
            self._set_parameter(generator, REPLY_PARAMETER, -token)

    def _cancel_transaction(self, marker):
        transaction = self._transactions.get(marker)
        if transaction is None:
            return True
        if self._job is not None and self._job.get("transaction") is transaction:
            # Its next scheduled step observes the revoked active token. Never delete a Browser
            # target while an in-flight job still owns it.
            return False
        if self._awaiting_commit is transaction:
            self._awaiting_commit = None
        try:
            if not transaction.discard():
                return False
            self._transactions.pop(marker, None)
            if getattr(self,"_history",None) and not getattr(transaction,"history_committing",False):
                self._history_event(dict(generator=transaction.generator,request_value=transaction.token),"terminal",
                                    outcome="failed" if getattr(transaction,"history_failed",False) else "cancelled")
        except Exception as exc:
            self.log_message("GeneratorPresetLoaderV2: incomplete instrument retained: %s" % exc)
            return False
        if self._alive and self._pending:
            self._begin_stability_wait()
        return True

    def _commit_transaction(self, generator, token):
        marker = _live_id(generator)
        transaction = self._transactions.get(marker)
        if not self._active(generator, token) or transaction is None or transaction.token != token:
            return
        if self._job is not None or self._awaiting_commit is not transaction:
            self._set_parameter(generator, COMMITTED_PARAMETER, -token)
            return
        success, error_code = False, 0
        try:
            transaction.commit(getattr(transaction, "roll_source", self._parameter_enabled(generator, DEVICES_PARAMETER))
                               or getattr(transaction, "roll_fx", False),
                               getattr(transaction, "roll_clip", self._parameter_enabled(generator, CLIP_PARAMETER)))
            success = True
        except Exception as exc:
            error_code = 6 if transaction.recovery else 3
            self.log_message("GeneratorPresetLoaderV2: commit failed: %s" % exc)
        self._awaiting_commit = None
        transaction.history_committing = True
        if not self._cancel_transaction(marker):
            error_code = 6 if transaction.recovery else 7
        self._set_parameter(generator, ERROR_PARAMETER, error_code)
        self._history_event(dict(generator=generator,request_value=token), "terminal",
                            outcome=("committed_with_warning" if error_code else "committed") if success else "failed", error_code=error_code)
        self._set_parameter(generator, COMMITTED_PARAMETER, token if success else -token)
        if self._pending:
            self._begin_stability_wait()

    def _make_request_callback(self, device, parameter):
        def callback():
            if not self._alive:
                return
            try:
                value = int(round(float(parameter.value)))
            except Exception:
                value = -1
            if self._request_gate.is_command(_live_id(device), value):
                self._queue_request(device, value)
        return callback

    def _clear_request_bindings(self):
        for parameter, callback in getattr(self, "_request_bindings", []):
            try:
                if parameter.value_has_listener(callback):
                    parameter.remove_value_listener(callback)
            except Exception:
                pass
        self._request_bindings = []

    def _clear_track_bindings(self):
        for track in getattr(self, "_track_bindings", []):
            try:
                if track.devices_has_listener(self._on_devices_changed):
                    track.remove_devices_listener(self._on_devices_changed)
            except Exception:
                pass
        self._track_bindings = []
        for device, event in getattr(self, "_tree_bindings", []):
            try:
                if getattr(device, event + "_has_listener")(self._on_devices_changed):
                    getattr(device, "remove_" + event + "_listener")(self._on_devices_changed)
            except Exception:
                pass
        self._tree_bindings = []

    # -- request/job state machine -----------------------------------------

    def _queue_request(self, generator, value):
        if not HOT_SWAP_ENABLED:
            self.log_message("GeneratorPresetLoaderV2: request ignored; Hot-Swap is quarantined")
            return
        marker = _live_id(generator)
        # More than one ROLL can arrive before stability. Keep only the newest token for a generator:
        # its newer roll has already replaced the older device tree.
        action = self._pending.push(generator, marker, value)
        self.log_message(
            "GeneratorPresetLoaderV2: request %d %s; waiting for the device tree to settle" %
            (value, action)
        )
        self._begin_stability_wait()

    def _open_job(self, generator, value):
        """Create the terminal-ack envelope before validating the mutable Live device tree."""
        request_parameter = self._find_parameter(generator, REQUEST_PARAMETER)
        completion_parameter = self._find_parameter(generator, COMPLETE_PARAMETER)
        return {
            "generator": generator,
            "completion_parameter": completion_parameter,
            "request_value": value,
            # Exact request/completion names are the immutable V2 capability contract. The listener
            # can only queue through REQUEST_PARAMETER; re-resolve both here because the tree is
            # mutable and terminal acknowledgement requires COMPLETE_PARAMETER.
            "protocol_ready": request_parameter is not None and completion_parameter is not None,
            "protocol_version": PROTOCOL_VERSION,
            "started_at": time.monotonic(),
            "plan": [],
            "position": 0,
            "loaded": 0,
            "failed": 0,
            "loaded_items": [],
            "preset_positions": frozenset(),
            "preset_target": 0,
        }

    def _begin_stability_wait(self):
        """Hold a queued load until the roll has finished mutating the device tree.

        The 2026-09-01 crash was a use-after-free: the load ran while devices were still being added
        and removed. Loads now start only after a run of churn-free ticks (StabilityGate), and never
        while a job is already running.
        """
        if self._stability_waiting or self._job is not None or self._awaiting_commit is not None or not self._pending:
            return
        self._stability_waiting = True
        self._stability.reset()
        self.schedule_message(1, self._await_stability)

    def _await_stability(self):
        if not self._alive or not self._pending:
            self._stability_waiting = False
            return
        self._stability.tick()
        if self._stability.is_stable():
            self._stability_waiting = False
            self.schedule_message(1, self._start_next_request)
        else:
            self.schedule_message(1, self._await_stability)

    def _start_next_request(self):
        if not self._alive or self._job is not None or self._awaiting_commit is not None or not self._pending:
            return
        generator, marker, value = self._pending.pop()
        self._job = self._open_job(generator, value)
        job = self._job
        job["marker"] = marker
        if not job["protocol_ready"]:
            job["failed"] += 1
            self._finish_job(
                "refused request %d: incomplete V2 request/completion bridge" % value
            )
            return
        located = self._locate_device(generator)
        if located is None:
            job["failed"] += 1
            self._finish_job("generator vanished before request %d" % value)
            return
        original, generator_index = located
        transaction = self._transactions.get(marker)
        if transaction is None or transaction.token != value or not self._active(generator, value):
            job["failed"] += 1
            self._finish_job("request canceled or roll grant unavailable")
            return
        try:
            transaction.verify()
        except Exception as exc:
            job["failed"] += 1
            self._finish_job("original changed before loading: %s" % exc)
            return
        job["transaction"] = transaction
        job["sound_types"] = getattr(transaction, "sound_types", None)
        track, owner, generator_index = transaction.original, transaction.container, transaction.index
        transaction.capture_generated()
        # Clip-only requests need no Browser work, but use the same commit/backup transaction.
        roll_source = getattr(transaction, "roll_source", self._parameter_enabled(generator, DEVICES_PARAMETER))
        roll_fx = getattr(transaction, "roll_fx", self._parameter_enabled(generator, DEVICES_PARAMETER))
        if not (roll_source or roll_fx):
            self._finish_job("clip prepared")
            return
        m4l_instrument = roll_source and self._parameter_enabled(generator, M4L_INSTRUMENT_ROLL_PARAMETER)
        vst_instrument = roll_source and self._parameter_enabled(generator, VST_ROLL)
        if vst_instrument and (m4l_instrument or not self._parameter_enabled(generator, VST_SOURCE)
                or self._parameter_value(generator, VST_READY, 0) != value
                or not getattr(self, "_vst_catalog", None) or not self._vst_catalog.candidates()):
            job["failed"] += 1
            self._finish_job("Rack preset source unavailable or switched off")
            return
        if m4l_instrument and not self._parameter_enabled(generator, M4L_INSTRUMENT_PARAMETER):
            job["failed"] += 1
            self._finish_job("M4L instrument source switched off; refused stale source flag")
            return
        after = list(owner.devices)[generator_index + 1:]
        if not after:
            job["failed"] += 1
            self._finish_job("no generated devices after generator")
            return

        # Same Pads prepares inside a muted parallel chain, never beside a track instrument.
        # The token and owned-chain checks keep old/mismatched runtimes off the preserved kit.
        stage_token = self._parameter_value(generator, PAD_STAGE_PARAMETER, 0)
        if stage_token:
            chains = list(safe_attr(after[0], "chains", ()))
            if (stage_token != value or len(after) != 1 or len(chains) != 2
                    or str(safe_attr(chains[1], "name", "")) != PAD_STAGE_NAME
                    or not bool(safe_attr(chains[1], "mute", False))):
                job["failed"] += 1
                self._finish_job("staged drum chain identity or isolation changed")
                return
            container, start = chains[1], 0
        # Normal rolls use the first chain (or the existing flat fallback).
        elif _is_rack(after[0]) and not ((m4l_instrument or vst_instrument) and not list(after[0].chains)):
            container = _first_chain(after[0])
            if container is None:
                job["failed"] += 1
                self._finish_job("generated Rack has no chain; skipping")
                return
            start = 0
        else:
            container = owner
            start = generator_index + 1
        creative = list(container.devices)[start:]
        if not creative:
            job["failed"] += 1
            self._finish_job("no generated devices in container")
            return

        # Infer the source that won this roll from the actual first generated device. The DEVICES
        # buttons are combinable, so an enabled Drums button does not mean this particular roll chose
        # drums. The generated empty Drum Rack advertises can_have_drum_pads before its kit is loaded.
        drums = _is_drum_rack(creative[0])
        plan = []
        if not roll_source:
            pass  # staging placeholder is never loaded; actual source stays in its original chain
        elif vst_instrument:
            if (str(safe_attr(creative[0], "class_name", "")) != "InstrumentGroupDevice"
                    or list(safe_attr(creative[0], "chains", ()))):
                job["failed"] += 1
                self._finish_job("VST placeholder changed")
                return
            plan.append((start, "vst", "Instrument Rack"))
        elif m4l_instrument:
            if (drums or not _is_rack(creative[0]) or list(creative[0].chains)
                    or str(safe_attr(creative[0], "class_name", "")) != "InstrumentGroupDevice"):
                job["failed"] += 1
                self._finish_job("M4L source placeholder changed; refused replacement")
                return
            plan.append((start, "m4l_instrument", "Instrument Rack"))
        elif drums and self._parameter_enabled(generator, KEEP_KIT_PARAMETER):
            # The old loaded Drum Rack is moved into this candidate after effect presets finish.
            # Loading a new kit into the placeholder would waste work and could disturb the
            # retained source; the Max side verifies its identity before making that move.
            pass
        elif drums:
            # Drums: the generator inserts an empty Drum Rack as the source slot (not a synth); the
            # loader fills it with a kit. Accept it without the native-instrument allowlist check.
            plan.append((start, "instrument", "Drum Rack"))
        else:
            instrument = _canonical_native_name(creative[0], INSTRUMENT_NAMES)
            if instrument is None:
                job["failed"] += 1
                self._finish_job(
                    "refused non-allowlisted instrument %r" % _labels(creative[0])
                )
                return
            plan.append((start, "instrument", instrument))
        for offset, device in enumerate(creative[1:] if roll_fx else [], start=start + 1):
            # Compressor is also a Pool effect; the device titles its safety utilities so a preset is
            # never loaded over them. Checked before type matching, which would say "Compressor".
            if _is_safety_utility(device):
                continue
            try:
                entry = self._effect_plan_entry(offset, device)
            except RuntimeError as exc:
                job["failed"] += 1
                self._finish_job(str(exc))
                return
            if entry is None:
                # Safety utilities (Compressor/Limiter) or unmatched devices — skip them, don't refuse
                # the whole roll.
                self.log_message("GeneratorPresetLoaderV2: skipping non-preset device %r" % _labels(device))
                continue
            plan.append(entry)

        use_user = self._parameter_enabled(generator, USER_PARAMETER)
        collection_ids = self._selected_collection_ids(generator)
        scope_only = self._parameter_enabled(generator, SCOPE_PARAMETER)  # True=only, False=include
        allow_m4l = self._parameter_enabled(generator, M4L_PARAMETER)  # audio effects only
        granular = self._parameter_enabled(generator, GRANULAR_PARAMETER)
        # In Gran the family dials decide; the equal/presets switch is greyed and not applied.
        m4l_fx_proportional = (self._parameter_enabled(generator, M4L_FX_WEIGHTING_PARAMETER)
                               and not granular)
        preset_ratio = self._parameter_value(generator, RATIO_PARAMETER, 100.0)
        source_requires_content = bool(roll_source and plan and plan[0][1] in ("instrument", "m4l_instrument")
                                       and (drums or m4l_instrument or plan[0][2] in SAMPLE_HOST_NAMES))
        category_source = bool(roll_source and plan and not drums and job.get("sound_types") is not None)
        required_positions = ([0] if source_requires_content or vst_instrument or category_source else [])
        required_positions += [i for i, entry in enumerate(plan) if entry[1] == "m4l_effect"]
        preset_positions = select_preset_positions(
            len(plan), preset_ratio, required_positions, self._rng.sample
        )
        factory_fallback = self._parameter_enabled(generator, FALLBACK_PARAMETER)
        try:
            browser = Live.Application.get_application().browser
        except Exception:
            browser = None
        # Personal sources need the User Library (a tagged preset may be user-owned) and the on-disk
        # (device, name) index. Browser Collection URIs are unstable, so membership comes from XMP.
        source_active = bool(use_user or collection_ids)
        user_root = self._browser_attr(browser, "user_library") if source_active else None
        preset_index = self._preset_index if source_active else None
        job.update({
            "track": track,
            "container": container,  # the track, or the generator's Instrument Rack chain
            "expected_count": len(list(container.devices)),
            "plan": plan,
            "preset_ratio": preset_ratio,
            "preset_positions": preset_positions,
            "preset_target": len(preset_positions),
            "factory_fallback": factory_fallback,
            "use_user": use_user,
            "collection_ids": collection_ids,
            "source_active": source_active,
            "scope_only": scope_only,
            "allow_m4l": allow_m4l,
            "m4l_fx_proportional": m4l_fx_proportional,
            "granular": granular,
            "drums": drums,
            "drum_plan_requested": self._parameter_value(generator, DRUM_PLAN_REQUEST, 0) == value,
            "drum_plan_done": False,
            "m4l_instrument": m4l_instrument,
            "vst_instrument": vst_instrument,
            "m4l_proportional": self._parameter_enabled(generator, M4L_WEIGHTING_PARAMETER),
            "vst_proportional": self._parameter_enabled(generator, VST_WEIGHTING_PARAMETER),
            "user_root": user_root,
            "preset_index": preset_index,
        })
        self._history_event(job, "plan", devices=[dict(index=i,kind=k,device=n) for i,k,n in plan],
                            preset_positions=sorted(preset_positions), preset_ratio=preset_ratio,
                            types=sorted(job["sound_types"]) if job.get("sound_types") is not None else None)
        self.log_message(
            "GeneratorPresetLoaderV2: request %d accepted (%d devices; preset slots=%s; pool=%s; "
            "weighting m4l=%s vst=%s m4l fx=%s; planned M4L effects=%d)" %
            (value, len(plan), ",".join(str(position + 1) for position in sorted(preset_positions))
             or "none", self._pool_description(job),
             "presets" if job["m4l_proportional"] else "equal",
             "presets" if job["vst_proportional"] else "equal",
             "gran" if job["granular"] else "presets" if job["m4l_fx_proportional"] else "equal",
             sum(1 for entry in plan if entry[1] == "m4l_effect"))
        )
        self.schedule_message(1, self._process_next_device)

    def _locate_device(self, wanted):
        for track in list(self.song().tracks):
            located = OnTrackRoll.location(track, wanted)
            if located is not None:
                return track, located[2]
        return None

    def _process_next_device(self):
        if not self._alive or self._job is None:
            return
        try:
            self._process_next_device_step()
        except Exception as exc:
            # A scheduled callback must always terminate its request envelope. Without this guard an
            # unexpected Python error leaves `_job` occupied forever and strands every later ROLL in
            # the pending queue (the empty-Simpler failure observed on 2026-09-07).
            job = self._job
            if job is not None:
                job["failed"] += 1
            self._leave_browse_mode()
            self.log_message(
                "GeneratorPresetLoaderV2: unexpected device-step failure: %r" % (exc,)
            )
            self._finish_job("unexpected device-step failure; aborted")

    def _process_next_device_step(self):
        """Process one planned device; `_process_next_device` owns the terminal exception guard."""
        job = self._job
        # Once insertion has started, even cancellation must observe the new Max device before
        # cleanup can delete it. The insertion state machine owns that bounded lifecycle.
        if job.get("m4l_insert") is not None:
            self._process_m4l_insertion(job)
            return
        if not self._active(job["generator"], job["request_value"]):
            job["failed"] += 1
            self._finish_job("request canceled; previous roll kept")
            return
        if time.monotonic() - job["started_at"] > 55:
            job["failed"] += 1
            self._finish_job("preset loading timed out")
            return
        if job.get("drum_plan_waiting") and not self._resume_drum_plan(job):
            return
        devices = list(job["container"].devices)  # the track, or the generated Rack's chain
        if len(devices) != job["expected_count"]:
            job["failed"] += 1
            self._finish_job("device count changed during Hot-Swap; aborted")
            return
        verification = job.pop("content_verification", None)
        if verification is not None:
            index, requirement = verification
            loaded = devices[index]
            failure = None
            if requirement == "rack":
                # An unfilled VST placeholder is a silent, empty Instrument Rack.
                try:
                    verify_loaded_rack(loaded, None)
                except ValueError:
                    failure = "VST Rack preset did not fill the placeholder (class=%s chains=%d)" % (
                        safe_attr(loaded, "class_name", None), len(list(safe_attr(loaded, "chains", ()))))
            elif requirement and _canonical_native_name(loaded, SAMPLE_HOST_NAMES) is None:
                failure = "sample source replacement has the wrong device type"
            failure = failure or _missing_sample_file(loaded)
            if failure:
                job["failed"] += 1
                job["loaded"] = max(0, job["loaded"] - 1)
                self._finish_job(failure)
                return
        if (job.get("drum_plan_requested") and not job.get("drum_plan_done")
                and job.get("drums") and job["position"] == 1):
            if job["failed"]:
                self._finish_job("drum kit failed before FX planning")
                return
            job["drum_plan_base_ids"] = [_live_id(device) for device in devices]
            job["drum_plan_waiting"] = True
            self._set_parameter(job["generator"], DRUM_KIT_READY, job["request_value"])
            self.schedule_message(1, self._process_next_device)
            return
        if job["position"] >= len(job["plan"]):
            self._finish_job("complete; loaded %d/%d presets" %
                             (job["loaded"], len(job["plan"])))
            return

        device_index, kind, canonical_name = job["plan"][job["position"]]
        if device_index >= len(devices):
            job["failed"] += 1
            self._finish_job("target index vanished; aborted")
            return
        target = devices[device_index]

        if kind == "vst":
            self._process_vst_recall(job, target)
            return

        if kind in ("m4l_instrument", "m4l_effect"):
            self._process_m4l_insertion(job, target)
            return

        needs_browser = ((kind == "instrument" and job.get("drums"))
                         or job["position"] in job.get("preset_positions", ()))
        if needs_browser:
            try:
                if not self._await_hot_swap_target(job, target):
                    self.schedule_message(1, self._process_next_device)
                    return
            except Exception as exc:
                job["failed"] += 1
                self._set_parameter(job["generator"], ERROR_PARAMETER, 9)
                self._finish_job("preset target unavailable: %s" % exc)
                return

        # Drums source: fill the source slot with a random Drum Rack KIT (Browser `load_item`). Done
        # BEFORE the native-instrument identity check, because the slot is an empty Drum Rack, not a
        # synth. Kits are never parameter-randomized; always loads (not preset-ratio-gated).
        if kind == "instrument" and job.get("drums"):
            try:
                kits = self._drum_kit_pool(
                    Live.Application.get_application().browser,
                    job.get("use_user"), job.get("collection_ids"), job.get("preset_index"),
                    job.get("scope_only"), job.get("factory_fallback"))
                if kits:
                    used = job.setdefault("used_uris", set())
                    def _kit_uri(item):
                        return str(safe_attr(item, "uri", "") or "") or str(id(item))
                    fresh = [k for k in kits if _kit_uri(k) not in used]
                    choices = fresh if fresh else kits
                    chosen = self._rng.choice(choices)
                    self._history_choice(job,"drum_preset",choices,[1]*len(choices),chosen)
                    used.add(_kit_uri(chosen))
                    self._hot_swap(job["track"], target, chosen)
                    job["content_verification"] = (device_index, False)
                    job["loaded"] += 1
                    job["loaded_items"].append(
                        "drums <- %s" % str(safe_attr(chosen, "name", item_description(chosen))))
                    self.log_message("GeneratorPresetLoaderV2: LOADED drum kit <- %s (from %d kits)" %
                                     (item_description(chosen), len(kits)))
                else:
                    job["failed"] += 1
                    self.log_message("GeneratorPresetLoaderV2: no drum kits in browser.drums; left as-is")
            except Exception as exc:
                job["failed"] += 1
                self._leave_browse_mode()
                self.log_message("GeneratorPresetLoaderV2: drum kit load failed: %r" % (exc,))
            job["position"] += 1
            self.schedule_message(2, self._process_next_device)
            return

        current_name = _canonical_native_name(
            target, INSTRUMENT_NAMES if kind == "instrument" else EFFECT_NAMES
        )
        if current_name != canonical_name:
            job["failed"] += 1
            self._finish_job("target identity changed at index %d; aborted" % device_index)
            return

        # Preset positions were allocated once for the whole roll. This makes 50% of four devices
        # exactly two, while still selecting a random subset without replacement. Simpler/Sampler are
        # content-bearing source hosts and position zero was made mandatory when the plan was built.
        content_required = kind == "instrument" and canonical_name in SAMPLE_HOST_NAMES
        if job["position"] not in job.get("preset_positions", ()):
            self._history_event(job, "preset_skipped", device=canonical_name, reason="random_parameters", kind=kind)
            self.log_message(
                "GeneratorPresetLoaderV2: kept random params for %s (roll preset quota)" %
                canonical_name
            )
            job["position"] += 1
            self.schedule_message(2, self._process_next_device)
            return

        try:
            browser = Live.Application.get_application().browser
            candidates, used_fallback, fill_replaced = self._native_preset_candidates(kind, canonical_name, job)
            if used_fallback:
                self.log_message("GeneratorPresetLoaderV2: factory fallback for %s" % canonical_name)

            if not candidates:
                if content_required or (kind == "instrument" and job.get("sound_types") is not None):
                    job["failed"] += 1
                self.log_message(
                    "GeneratorPresetLoaderV2: no %s preset for %s%s%s" %
                    (self._pool_description(job), canonical_name,
                     " (only mode, skipped)" if job.get("scope_only") else "",
                     " — required sample content missing" if content_required else "")
                )
            else:
                # Avoid loading the same preset twice in one chain while options remain: prefer
                # candidates not yet used this roll, only repeating once the pool is exhausted (small
                # user pools otherwise show as e.g. four identical "Reverb" devices).
                used = job.setdefault("used_uris", set())
                def _uri(item):
                    return str(safe_attr(item, "uri", "") or "") or str(id(item))
                fresh = [c for c in candidates if _uri(c) not in used]
                choices = fresh if fresh else candidates
                chosen = self._rng.choice(choices)
                self._history_choice(job,"native_preset",choices,[1]*len(choices),chosen)
                used.add(_uri(chosen))
                self._hot_swap(job["track"], target, chosen)
                if kind == "instrument":
                    job["content_verification"] = (device_index, content_required)
                job["loaded"] += 1
                preset_name = str(safe_attr(chosen, "name", item_description(chosen)))
                # In only-fill the loaded preset REPLACES the generated device with one of yours, so
                # label the slot by the preset (not the old device) to avoid a misleading summary.
                job["loaded_items"].append(
                    "%s <- %s" % ("(replaced) " + canonical_name if fill_replaced else canonical_name,
                                  preset_name)
                )
                self.log_message(
                    "GeneratorPresetLoaderV2: LOADED %s <- %s (%s%s%s; from %d candidates)" %
                    (canonical_name, item_description(chosen), self._pool_description(job),
                     ", factory fallback" if used_fallback else "",
                     ", only-fill (device replaced)" if fill_replaced else "", len(candidates))
                )
        except Exception as exc:
            job["failed"] += 1
            self._leave_browse_mode()
            self.log_message("GeneratorPresetLoaderV2: %s preset failed: %r" % (canonical_name, exc))

        job["position"] += 1
        # Live may replace the Device object asynchronously; refresh on a later control-surface tick.
        self.schedule_message(2, self._process_next_device)

    def _resume_drum_plan(self, job):
        value = self._parameter_value(job["generator"], DRUM_PLAN_READY, 0)
        token = job["request_value"]
        if value == -token:
            job["failed"] += 1
            self._finish_job("drum FX classification failed")
            return False
        if value != token:
            self.schedule_message(1, self._process_next_device)
            return False
        job["transaction"].verify()
        devices = list(job["container"].devices)
        base = job["drum_plan_base_ids"]
        if [_live_id(device) for device in devices[:len(base)]] != base:
            raise RuntimeError("drum source changed during FX planning")
        plan = list(job["plan"][:1])  # the one already loaded/verified kit
        for index in range(len(base), len(devices)):
            if _is_safety_utility(devices[index]):
                continue
            entry = self._effect_plan_entry(index, devices[index])
            if entry is not None:
                plan.append(entry)
            elif _canonical_native_name(devices[index], ("Compressor", "Limiter")) is None:
                raise RuntimeError("unexpected device after drum classification")
        job["plan"] = plan
        job["expected_count"] = len(devices)
        required = [0] + [i for i, entry in enumerate(plan) if entry[1] == "m4l_effect"]
        job["preset_positions"] = select_preset_positions(len(plan), job["preset_ratio"], required, self._rng.sample)
        job["preset_target"] = len(job["preset_positions"])
        job["drum_plan_done"] = True
        job["drum_plan_waiting"] = False
        self._history_event(job,"plan",devices=[dict(index=i,kind=k,device=n) for i,k,n in plan],
                            preset_positions=sorted(job["preset_positions"]),deferred_drums=True)
        self.log_message("GeneratorPresetLoaderV2: classified drum plan request=%d effects=%d" % (token, len(plan) - 1))
        return True

    def _load_checkpoint(self, job, operation, state):
        planned = job.get("plan") or []
        position = job.get("position",0)
        device = state.get("source") or (planned[position][2] if position<len(planned) else None)
        self._history_event(job, "load", operation=operation, device=device,
                            preset=state.get("description"), target=str(state.get("target")))
        # Only captured primitive context: logging must not inspect a target that load_item deleted.
        self.log_message("GeneratorPresetLoaderV2: LOAD request=%s slot=%s op=%s target=%s source=%s preset=%s" % (
            job["request_value"], job["position"], operation, state.get("target", "none"),
            state.get("source", "none"), state.get("description", "")))

    def _m4l_hot_swap(self, job, target, state):
        """The alpha.3 M4L actuator: let Live appoint the target through normal Browser mode.

        Used for M4L instruments and, since dev.26, M4L audio effects. Directly assigning
        browser.hotswap_target before loading a Max device crashed Live twice (2026-09-13 instrument,
        2026-11-01 Plate Reverb); this Browser-mode route has loaded Poli/Bass without a crash."""
        effect = state.get("kind") == "effect"
        self._verify_browser_owner(job, job["track"], target)
        app = Live.Application.get_application()
        browser, song_view = app.browser, self.song().view
        if _live_id(song_view.selected_track) != _live_id(job["track"]):
            raise RuntimeError("selected track changed; M4L load canceled")
        self._leave_browse_mode()  # releases only our previous native preset target
        if browser.hotswap_target is not None:
            raise RuntimeError("Browser Hot-Swap is already in use")
        previous_device = job["track"].view.selected_device
        entered = False
        try:
            if bool(app.view.browse_mode):
                app.view.toggle_browse()
            self._load_checkpoint(job, "m4l.select.begin", state)
            song_view.select_device(target)
            self._load_checkpoint(job, "m4l.select.returned", state)
            self._load_checkpoint(job, "m4l.browse.begin", state)
            entered = True
            app.view.toggle_browse()
            self._load_checkpoint(job, "m4l.browse.returned", state)
            hot_target = browser.hotswap_target
            if hot_target is None or _live_id(hot_target) != state["target"]:
                raise RuntimeError("Live did not appoint the M4L placeholder; expected=%s actual=%s" %
                                   (state["target"], _live_id(hot_target) if hot_target is not None else None))
            self._verify_browser_owner(job, job["track"], target)
            if _live_id(song_view.selected_track) != _live_id(job["track"]):
                raise RuntimeError("selected track changed before M4L load")
            transaction = job["transaction"]
            if not effect:
                transaction.source_busy = True
                transaction.pending_container = job["container"]
                transaction.pending_root = list(transaction.container.devices)[transaction.index + 1]
            self._load_checkpoint(job, "m4l.load.begin", state)
            state["load_started"] = True
            started = time.monotonic()
            try:
                browser.load_item(state["item"])
            finally:
                job["browser_load_ms"] = job.get("browser_load_ms", 0.0) + (
                    time.monotonic() - started) * 1000.0
                # In flat mode the replaced placeholder was itself the generated root.
                # Keep the published replacement protected even if load_item raised late.
                if not effect and job["container"] is transaction.container:
                    devices = list(transaction.container.devices)
                    if len(devices) > transaction.index + 1:
                        transaction.pending_root = devices[transaction.index + 1]
            self._load_checkpoint(job, "m4l.load.returned", state)
        finally:
            # Use the same normal-mode exit as alpha.3, not alpha.7's direct target setter.
            # No old-target dereference or Max-device display write after load_item.
            if entered:
                self._load_checkpoint(job, "m4l.browse.close.begin", state)
                if bool(app.view.browse_mode):
                    app.view.toggle_browse()
                self._load_checkpoint(job, "m4l.browse.close.returned", state)
            if (_live_id(song_view.selected_track) == _live_id(job["track"])
                    and previous_device is not None and bool(previous_device)):
                current = job["track"].view.selected_device
                owned = [state["target"]] + [_live_id(d) for d in job["container"].devices
                                             if _live_id(d) not in state["before"]]
                if _live_id(current) in owned:
                    song_view.select_device(previous_device)

    def _process_m4l_insertion(self, job, target=None):
        """Restore the working M4L replacement path; keep bounded observation of the loaded source."""
        transaction = job["transaction"]
        state = job.get("m4l_insert")
        try:
            transaction.verify()
            container = job["container"]
            if not transaction.contains(container):
                raise RuntimeError("M4L container moved outside the owned device chain")
            if state is None:
                self._verify_browser_owner(job, job["track"], target)
                entry = job["plan"][job["position"]]
                effect = entry[1] == "m4l_effect"
                if effect:
                    if self._effect_plan_entry(entry[0], target) != entry:
                        raise RuntimeError("planned M4L effect placeholder changed")
                    family = entry[2]
                    items = self._m4l_effect_groups((family,)).get(family, ())
                    if not items:
                        raise RuntimeError("planned M4L effect unavailable: " + family)
                    used = job.setdefault("used_uris", set())
                    def uri(item):
                        return str(safe_attr(item, "uri", "") or "") or str(id(item))
                    choices = [item for item in items if uri(item) not in used] or items
                    chosen = self._rng.choice(choices)
                    self._history_choice(job,"m4l_fx_preset",choices,[1]*len(choices),chosen)
                    used.add(uri(chosen))
                else:
                    if (str(safe_attr(target, "class_name", "")) != "InstrumentGroupDevice"
                            or list(safe_attr(target, "chains", ()))):
                        raise RuntimeError("M4L Instrument Rack placeholder changed")
                    selected = choose_instrument_preset(
                        self._m4l_instrument_pools(job), self._rng.choice,
                        self._m4l_family_counts() if job.get("m4l_proportional") else None, self._rng.random,
                        lambda stage,items,weights,chosen: self._history_choice(job,stage,items,weights,chosen))
                    if selected is None:
                        raise RuntimeError("no matching M4L instrument presets in selected sources")
                    family, chosen = selected
                state = dict(family=family, item=chosen, source=family, description=item_description(chosen),
                             target=_live_id(target), index=job["plan"][job["position"]][0],
                             before=[_live_id(device) for device in container.devices],
                             observed=None, checks=0, started=time.monotonic())
                if effect:
                    state.update(kind="effect", name=str(safe_attr(chosen, "name", item_description(chosen))),
                                 installed=len(items))
                job["m4l_insert"] = state
                self._m4l_hot_swap(job, target, state)
                state["started"] = time.monotonic()
                self.schedule_message(2, self._process_next_device)
                return

            effect = state.get("kind") == "effect"
            state["checks"] += 1
            devices = list(container.devices)
            index = state["index"]
            if not effect and container is transaction.container and len(devices) > index:
                transaction.pending_root = devices[index]
            ids = [_live_id(device) for device in devices]
            before = state["before"]
            if (len(ids) != len(before) or ids[:index] != before[:index]
                    or ids[index + 1:] != before[index + 1:]):
                raise RuntimeError("M4L replacement changed unexpected devices")
            signature = None
            loaded = devices[index]
            expected = "MxDeviceAudioEffect" if effect else "MxDeviceInstrument"
            if bool(loaded) and str(safe_attr(loaded, "class_name", "")) == expected:
                parameters = list(safe_attr(loaded, "parameters", ()))
                if len(parameters) > 1 and all(bool(parameter) for parameter in parameters):
                    signature = (_live_id(loaded), tuple(
                        (_live_id(parameter), str(parameter.name)) for parameter in parameters))
            ready = signature is not None and signature == state["observed"]
            state["observed"] = signature
            if not ready:
                if time.monotonic() - state["started"] >= 5.0 or state["checks"] >= 50:
                    raise RuntimeError("M4L replacement not confirmed; MIDI unchanged")
                self.schedule_message(1, self._process_next_device)
                return
            if not effect:
                transaction.source_busy = False
            self._load_checkpoint(job, "m4l.verified", state)
            if (not self._active(job["generator"], job["request_value"])
                    or time.monotonic() - job["started_at"] > 55):
                raise RuntimeError("M4L request canceled; MIDI unchanged")
            job["loaded"] += 1
            if effect:
                job["loaded_items"].append("%s <- (m4l) %s" % (state["family"], state["name"]))
                self.log_message("GeneratorPresetLoaderV2: LOADED M4L %s (from %d installed; Browser mode)" %
                                 (state["description"], state["installed"]))
            else:
                job["loaded_items"].append("%s <- %s" % (state["family"], state["description"]))
                self.log_message("GeneratorPresetLoaderV2: LOADED M4L instrument %s <- %s (on track)" %
                                 (state["family"], state["description"]))
            job.pop("m4l_insert")
            job["position"] += 1
            self.schedule_message(2, self._process_next_device)
        except Exception as exc:
            job["failed"] += 1
            if transaction.source_busy:
                self._set_parameter(job["generator"], ERROR_PARAMETER, 7)
            what = "M4L effect" if ((state is not None and state.get("kind") == "effect")
                or job["plan"][job["position"]][1] == "m4l_effect") else "M4L instrument"
            self._finish_job("%s load failed: %r; no native substitute" % (what, exc))

    def _process_vst_recall(self, job, target):
        """Load an existing Rack preset the way native presets and drum kits are loaded."""
        catalog = getattr(self, "_vst_catalog", None)
        try:
            if not self._parameter_enabled(job["generator"], VST_SOURCE):
                raise RuntimeError("VST source switched off")
            if job.get("vst_entry") and (not catalog or job["vst_entry"] not in catalog.candidates()):
                raise RuntimeError("VST preset excluded or quarantined while loading")
            if not catalog or not catalog.candidates():
                raise RuntimeError("no enabled Rack preset")
            if "vst_entry" not in job:
                job["vst_entry"] = choose_preset(catalog, self._rng, job.get("sound_types"),
                                                 job.get("vst_proportional", False),
                                                 lambda stage,items,weights,chosen: self._history_choice(job,stage,items,weights,chosen))
            if not self._await_hot_swap_target(job, target):
                self.schedule_message(1, self._process_next_device)
                return
            item = catalog.revalidate(job["vst_entry"])
            self._hot_swap(job["track"], target, item)
            # As for native presets: count it, and the next step checks the chain and its content.
            job["content_verification"] = (job["plan"][job["position"]][0], "rack")
            job["loaded"] += 1
            job.setdefault("loaded_items", []).append("Rack preset: " + job["vst_entry"]["path"])
            self.log_message("GeneratorPresetLoaderV2: LOADED VST Rack <- %s (%s)" %
                             (job["vst_entry"]["path"], item_description(item)))
            job["position"] += 1
            self.schedule_message(2, self._process_next_device)
        except Exception as exc:
            job["failed"] += 1
            self._leave_browse_mode()
            self._finish_job("Rack preset recall failed: %s" % exc)

    def _verify_browser_owner(self, job, track, target):
        if self._job is not job or not self._active(job["generator"], job["request_value"]):
            raise RuntimeError("stale load request")
        transaction = job.get("transaction")
        if transaction is None or transaction.original != track:
            raise RuntimeError("Browser target is not this request's original track")
        transaction.verify()
        if not transaction.contains(job["container"]):
            raise RuntimeError("Browser container left the owned device chain")
        devices = list(job["container"].devices)
        index = job["plan"][job["position"]][0]
        if index >= len(devices) or _live_id(devices[index]) != _live_id(target):
            raise RuntimeError("Browser target moved or changed")

    def _target_diagnostic(self, job, target, app):
        def describe(obj):
            return "none" if obj is None else "%s:%s:%s" % (
                type(obj).__name__, _live_id(obj), "/".join(_labels(obj)))
        return "request=%s phase=%s expected=%s actual=%s track=%s selected=%s browse=%s" % (
            job["request_value"], job.get("hot_swap", {}).get("phase", "none"),
            describe(target), describe(safe_attr(app.browser, "hotswap_target", None)),
            describe(safe_attr(self.song().view, "selected_track", None)),
            describe(safe_attr(safe_attr(job["track"], "view", None), "selected_device", None)),
            safe_attr(app.view, "browse_mode", None))

    def _await_hot_swap_target(self, job, target):
        """Bind the exact nested Device, independently of which Rack the UI displays."""
        track = job["track"]
        self._verify_browser_owner(job, track, target)
        app = Live.Application.get_application()
        state = job.get("hot_swap")
        if state is None or state["position"] != job["position"]:
            self._leave_browse_mode()
            state = dict(position=job["position"], target=_live_id(target),
                         phase="direct-target", started=time.monotonic(), checks=0)
            job["hot_swap"] = state
            if _live_id(self.song().view.selected_track) != _live_id(track):
                raise RuntimeError("selected track changed; load canceled")
            # Ableton's BrowserHotswapMode sets this property directly. toggle_browse infers a
            # destination from UI focus and can retarget a selected child to its enclosing Rack.
            self._owned_browser_target = _live_id(target)
            app.browser.hotswap_target = target
            actual = safe_attr(app.browser, "hotswap_target", None)
            return actual is not None and _live_id(actual) == _live_id(target)
        state["checks"] += 1
        if state["target"] != _live_id(target):
            raise RuntimeError("target identity changed while waiting")
        if time.monotonic() - state["started"] >= 2.0 or state["checks"] >= 20:
            raise RuntimeError("target readiness timed out; " + self._target_diagnostic(job, target, app))
        if _live_id(self.song().view.selected_track) != _live_id(track):
            raise RuntimeError("selected track changed; " + self._target_diagnostic(job, target, app))
        hot_target = safe_attr(app.browser, "hotswap_target", None)
        return hot_target is not None and _live_id(hot_target) == _live_id(target)

    def _hot_swap(self, track, target, preset_item):
        job = self._job
        if job is None:
            raise RuntimeError("stale load request")
        self._verify_browser_owner(job, track, target)
        app = Live.Application.get_application()
        browser = app.browser
        hot_target = safe_attr(browser, "hotswap_target", None)
        if (_live_id(self.song().view.selected_track) != _live_id(track)
                or hot_target is None or _live_id(hot_target) != _live_id(target)):
            self._set_parameter(job["generator"], ERROR_PARAMETER, 9)
            detail = self._target_diagnostic(job, target, app)
            self._leave_browse_mode()
            raise RuntimeError("Hot-Swap target changed before load; " + detail)
        started = time.monotonic()
        context = dict(description=item_description(preset_item), target=_live_id(target))
        try:
            self._load_checkpoint(job, "hotswap.load.begin", context)
            browser.load_item(preset_item)
            self._load_checkpoint(job, "hotswap.load.returned", context)
        finally:
            if self._job is not None:
                self._job["browser_load_ms"] = self._job.get("browser_load_ms", 0.0) + (
                    time.monotonic() - started) * 1000.0
            self._load_checkpoint(job, "hotswap.cleanup.begin", context)
            self._leave_browse_mode()
            self._load_checkpoint(job, "hotswap.cleanup.returned", context)

    def _leave_browse_mode(self):
        owned = getattr(self, "_owned_browser_target", None)
        self._owned_browser_target = None
        if owned is None:
            return
        try:
            browser = Live.Application.get_application().browser
            target = browser.hotswap_target
            if target is not None and _live_id(target) == owned:
                browser.hotswap_target = None
        except Exception as exc:
            self.log_message("GeneratorPresetLoaderV2: owned Browser target cleanup failed: %r" % exc)

    @staticmethod
    def _find_parameter(device, name):
        for parameter in list(getattr(device, "parameters", ())):
            try:
                if str(parameter.name) == name:
                    return parameter
            except Exception:
                pass
        return None

    @classmethod
    def _parameter_enabled(cls, device, name):
        parameter = cls._find_parameter(device, name)
        if parameter is None:
            return False
        try:
            return float(parameter.value) >= 0.5
        except Exception:
            return False

    @classmethod
    def _parameter_value(cls, device, name, default):
        parameter = cls._find_parameter(device, name)
        value = safe_attr(parameter, "value", None) if parameter is not None else None
        return numeric_snapshot(value, default)

    @classmethod
    def _selected_collection_ids(cls, device):
        """Snapshot the seven independent Collection parameters once for an accepted request."""
        return tuple(color_id for parameter, color_id, _label in COLLECTION_PARAMETERS
                     if cls._parameter_enabled(device, parameter))

    @staticmethod
    def _browser_attr(browser, name):
        try:
            return getattr(browser, name)
        except Exception:
            return None

    @staticmethod
    def _pool_description(job):
        """Human-readable factory/User/Collection policy label for diagnostic logs."""
        use_user = job.get("use_user")
        collection_ids = job.get("collection_ids") or ()
        if not (use_user or collection_ids):
            return "all"
        names = (["user"] if use_user else []) + [
            COLLECTION_NAMES_BY_ID.get(color_id, "collection-" + str(color_id))
            for color_id in collection_ids
        ]
        sources = "+".join(names)
        return "%s %s" % (sources, "only" if job.get("scope_only") else "include")

    def _signal_completion(self, job):
        parameter = job.get("completion_parameter")
        request_value = job.get("request_value")
        if parameter is None or request_value is None:
            return
        try:
            self._set_parameter(job["generator"], RESULT_PARAMETER,
                                -request_value if job.get("failed") else request_value)
            parameter.value = float(request_value)
            self.log_message("GeneratorPresetLoaderV2: acknowledged request %d" % request_value)
        except Exception as exc:
            self.log_message("GeneratorPresetLoaderV2: completion signal failed: %r" % (exc,))

    def _finish_job(self, message):
        job = self._job
        self._job = None
        self._leave_browse_mode()
        if message:
            self.log_message("GeneratorPresetLoaderV2: %s" % message)
        if job is not None:
            self._history_event(job, "loads_finished", loaded=job.get("loaded",0), failed=job.get("failed",0),
                                presets=job.get("loaded_items",[]), detail=message)
            if job.get("failed") and job.get("vst_entry") and getattr(self, "_vst_catalog", None):
                self._vst_catalog.quarantine(job["vst_entry"])
                try:
                    available = len(self._vst_catalog.candidates()) or -5
                    if hasattr(self._vst_catalog, "root"):
                        write_pool_catalog(self._vst_catalog.root, self._vst_catalog, available)
                except Exception as exc:
                    available = -6
                    self.log_message("GeneratorPresetLoaderV2: VST Pool update failed: %s" % exc)
                try:
                    self._set_parameter(job["generator"], VST_AVAILABLE, available)
                except Exception as exc:
                    self.log_message("GeneratorPresetLoaderV2: VST status update failed: %s" % exc)
            # One clear summary line per roll listing exactly what was loaded (grep this).
            items = job.get("loaded_items") or []
            self.log_message(
                "GeneratorPresetLoaderV2: PRESETS THIS ROLL (%d): %s" %
                (len(items), " | ".join(items) if items else "(none)")
            )
            elapsed_ms = (time.monotonic() - job.get("started_at", time.monotonic())) * 1000.0
            self.log_message(
                "GeneratorPresetLoaderV2: PERF version=%d request=%d companion_ms=%.1f planned=%d "
                "preset_target=%d loaded=%d failed=%d lookup_ms=%.1f browser_load_ms=%.1f" % (
                    job.get("protocol_version", PROTOCOL_VERSION),
                    job.get("request_value", -1), elapsed_ms,
                    len(job.get("plan") or ()), job.get("preset_target", 0),
                    job.get("loaded", 0), job.get("failed", 0),
                    job.get("lookup_ms", 0.0), job.get("browser_load_ms", 0.0),
                )
            )
            transaction = job.get("transaction")
            if job.get("failed"):
                self._history_event(job,"terminal",outcome="failed",detail=message)
                if transaction is not None: transaction.history_failed=True
            if transaction is not None and not job.get("failed") and self._active(job["generator"], transaction.token):
                transaction.ready = True
                self._awaiting_commit = transaction
            elif transaction is not None:
                if not self._cancel_transaction(job.get("marker", _live_id(job["generator"]))):
                    self._set_parameter(job["generator"], ERROR_PARAMETER, 7)
                    self.log_message("GeneratorPresetLoaderV2: request %d incomplete instrument retained" %
                                     job["request_value"])
            self._signal_completion(job)
        if self._alive and self._pending:
            # Loading presets replaced devices, so let the tree settle before the next request too.
            self._begin_stability_wait()
