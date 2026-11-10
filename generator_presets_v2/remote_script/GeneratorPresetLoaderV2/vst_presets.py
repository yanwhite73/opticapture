"""Recall existing User Library Rack presets through Live's ordinary Browser.

Discovery lists filenames using the native preset directory iterator. No preset
body is read, parsed, hashed or converted. Grouping comes from the folder layout
the rack-preset-kit documents (vendor/instrument/category/preset.adg); Live owns
decoding and loading the chosen .adg. No plug-in lookup, tools or database.
"""
import json
import math
from pathlib import Path, PurePosixPath
import time

try:
    from .preset_categories import filter_vst
    from .disk_index import iter_preset_paths
    from .browser_utils import is_excluded_name
except ImportError:  # desktop tests
    from preset_categories import filter_vst
    from disk_index import iter_preset_paths
    from browser_utils import is_excluded_name

POOL_SCHEMA = "optimystery.vst-pool.v2"
SELECTION_FILE = "vst-pool-selection-v2.json"
MAX_ENTRIES = 20000
SKIP_EXAMPLES = 3
# Live's own instrument folders hold native presets, never plug-in Racks.
LIVE_INSTRUMENT_FOLDERS = frozenset(name.casefold() for name in (
    "Analog", "Collision", "Drift", "Drum Rack", "Drum Sampler", "Electric", "External Instrument",
    "Impulse", "Instrument Rack", "Max Instrument", "Meld", "Operator", "Sampler", "Simpler",
    "Tension", "Wavetable"))


class VstUnavailable(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def pool_folder(root):
    return Path(root) / "Max MIDI Effect/Optimystery"


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def valid_path(value, preset=False):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return False
    parts = value.split("/")
    return (parts[:2] == ["Presets", "Instruments"] and len(parts) >= 3
            and not any(p in ("", ".", "..") for p in parts)
            and not any(ord(c) < 32 or c == "\\" for c in value)
            and (not preset or value.lower().endswith(".adg")))


def pool_selection(root):
    path = pool_folder(root) / SELECTION_FILE
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("settings exceed size limit")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(schema=POOL_SCHEMA, enabled_plugins=[], excluded_plugins=[], excluded_presets=[])
    except (OSError, ValueError) as exc:
        raise VstUnavailable(-6, "Rack Pool settings unreadable") from exc
    if not isinstance(value, dict) or value.get("schema") != POOL_SCHEMA:
        raise VstUnavailable(-6, "Invalid Rack Pool settings")
    for name, limit in (("enabled_plugins", 256), ("excluded_plugins", 256), ("excluded_presets", MAX_ENTRIES)):
        items = value.get(name)
        if (not isinstance(items, list) or len(items) > limit
                or any(not valid_path(item, name == "excluded_presets") for item in items)):
            raise VstUnavailable(-6, "Invalid Rack Pool selection")
    weights = value.get("plugin_weights", {})
    if (not isinstance(weights, dict) or len(weights) > 256 or any(
            not valid_path(key) or isinstance(weight, bool) or not isinstance(weight, (int, float))
            or not math.isfinite(weight) or not 0 <= weight <= 100 for key, weight in weights.items())):
        raise VstUnavailable(-6, "Invalid Rack Pool weights")
    for field, preset in (("solo_plugin", False), ("solo_preset", True)):
        solo = value.get(field, "")
        if solo != "" and not valid_path(solo, preset):
            raise VstUnavailable(-6, "Invalid Rack Pool solo")
    if value.get("solo_plugin") and value.get("solo_preset"):
        raise VstUnavailable(-6, "Conflicting Rack Pool solos")
    return value


def plugin_key(entry):
    return entry["plugin_key"]  # existing instrument folder, never an inferred VST UID


def write_pool_catalog(root, catalog, code):
    entries = [] if catalog is None else [dict(entry, quarantined=entry["path"] in catalog.blocked)
                                          for entry in catalog.entries]
    atomic_json(pool_folder(root) / "vst-pool-catalog.json",
                dict(schema=POOL_SCHEMA, status=code, entries=entries,
                     skipped=getattr(catalog, "skipped", {})))


SOURCE_COUNTS_SCHEMA = "optimystery.source-counts.v1"
SOURCE_COUNTS_FILE = "source-preset-counts.json"


def write_source_counts(root, instruments, sample_hosts, m4l_instruments, drum_kits, audio_effects=None):
    """Startup-list preset counts per source device; the device weights its source draw by them."""
    payload = dict(schema=SOURCE_COUNTS_SCHEMA, instruments=dict(instruments),
                   sample_hosts=dict(sample_hosts), m4l_instruments=dict(m4l_instruments),
                   drum_kits=int(drum_kits))
    if audio_effects is not None:
        payload['audio_effects'] = dict(audio_effects)
    atomic_json(pool_folder(root) / SOURCE_COUNTS_FILE, payload)


def instrument_group(relative):
    """Return ``(plugin_key, label)`` from the folder layout, or ``(None, skip reason)``."""
    parts = relative.split("/")
    if not valid_path(relative, True):
        return None, "invalid path"
    if is_excluded_name(parts[-1]):
        return None, "excluded name"
    if parts[2].casefold() in LIVE_INSTRUMENT_FOLDERS:
        return None, "Live instrument folder"
    # Saved Optimystery rolls include whole instrument/effect chains, not the
    # vendor/instrument output. Match the root directory, including all categories.
    if parts[2].casefold() == "optimystery":
        return None, "saved Optimystery Rack folder"
    if any(part.startswith("_") for part in parts[2:-1]):
        return None, "metadata or test folder"
    if len(parts) < 5:
        return None, "not in a vendor/instrument folder"
    return "/".join(parts[:4]), parts[3]


def browser_path(root, relative):
    """Resolve one exact path, allowing Live to hide the final .adg extension."""
    item = root
    parts = PurePosixPath(relative).parts
    for index, name in enumerate(parts):
        names = {name, name[:-4]} if index == len(parts) - 1 else {name}
        children = list(getattr(item, "children", ()) or ())
        matches = [child for child in children if str(getattr(child, "name", "")) in names]
        if len(matches) != 1:
            seen = sorted(str(getattr(child, "name", "")) for child in children)[:5]
            raise ValueError("Rack Browser path missing or ambiguous at %r: %d matches among %d "
                             "children, e.g. %r" % (name, len(matches), len(children), seen))
        item = matches[0]
    if (not bool(getattr(item, "is_loadable", False)) or bool(getattr(item, "is_device", False))
            or not str(getattr(item, "uri", ""))):
        raise ValueError("Rack is not currently loadable in Live's Browser")
    return item


class RackPresets:
    def __init__(self, root, browser, blocked=()):
        self.root, self.browser = Path(root), browser
        folder = self.root / "Presets/Instruments"
        if not folder.is_dir():
            raise VstUnavailable(-2, "User Library Instruments folder unavailable")
        folder.resolve(strict=True).relative_to(self.root.resolve(strict=True))
        self.entries, self.blocked, self.skipped = [], set(blocked), {}
        self.processed, self.total, self.complete = 0, 0, False
        self.started, self.callback_ms = time.monotonic(), 0.0
        self.skipped_examples = {}
        self._paths = iter_preset_paths(self.root, ("Instruments",))

    def advance(self, count=1024):
        deadline = time.monotonic() + 0.008
        for _ in range(count):
            try:
                relative = next(self._paths)
            except StopIteration:
                self.complete, self.total = True, self.processed
                return True
            if relative is not None and relative.lower().endswith(".adg"):
                self.processed += 1
                if self.processed > MAX_ENTRIES:
                    raise ValueError("Rack preset count exceeds bound")
                key, label = instrument_group(relative)
                if key is not None:
                    self.entries.append(dict(path=relative, name=relative.split("/")[-1][:-4],
                                             plugin_key=key, plugin_name=label))
                else:
                    self.skipped[label] = self.skipped.get(label, 0) + 1
                    examples = self.skipped_examples.setdefault(label, [])
                    if len(examples) < SKIP_EXAMPLES:
                        examples.append(relative)
            if time.monotonic() >= deadline:
                return False
        return False

    def cancel(self):
        self._paths.close()

    def candidates(self, selection=None):
        if not self.complete:
            return []
        selection = pool_selection(self.root) if selection is None else selection
        # Every listed instrument is on unless excluded; enabled_plugins is legacy and unread.
        off = set(selection["excluded_plugins"])
        excluded = set(selection["excluded_presets"]) | self.blocked
        weights = selection.get("plugin_weights", {})
        return [e for e in self.entries if e["plugin_key"] not in off and e["path"] not in excluded
                and (not selection.get("solo_plugin") or e["plugin_key"] == selection["solo_plugin"])
                and (not selection.get("solo_preset") or e["path"] == selection["solo_preset"])
                and weights.get(e["plugin_key"], 100) > 0]

    def selection_candidates(self):
        selection = pool_selection(self.root)
        return selection, self.candidates(selection)

    def revalidate(self, entry):
        if entry not in self.candidates() or not valid_path(entry.get("path"), True):
            raise ValueError("Rack preset excluded or unavailable")
        # Match normal preset loading: check the chosen location, then let Live
        # read its current contents. No content digest or XML qualification gate.
        path = (self.root / entry["path"]).resolve(strict=True)
        path.relative_to(self.root.resolve(strict=True))
        if not path.is_file():
            raise ValueError("Rack preset file missing")
        return browser_path(self.browser.user_library, entry["path"])

    def quarantine(self, entry):
        self.blocked.add(entry["path"])


def choose_preset(catalog, rng, categories=None, proportional=False, observer=None):
    selection, included = catalog.selection_candidates()
    entries = filter_vst(included, categories)
    groups = {}
    for entry in entries:
        groups.setdefault(entry["plugin_key"], []).append(entry)
    if not groups:
        raise ValueError("Rack Pool empty")
    keys = sorted(groups)
    weights = [selection.get("plugin_weights", {}).get(key, 100) for key in keys]
    if proportional:
        # Presets weighting: an instrument counts its included listing, before sound types.
        sizes = {}
        for entry in included:
            sizes[entry["plugin_key"]] = sizes.get(entry["plugin_key"], 0) + 1
        weights = [weight * sizes.get(key, 0) for key, weight in zip(keys, weights)]
    if len(set(weights)) == 1:
        chosen = rng.choice(keys)  # preserve default/equal-weight RNG behavior exactly
    else:
        threshold = rng.random() * sum(weights)
        chosen = keys[-1]
        for key, weight in zip(keys, weights):
            threshold -= weight
            if threshold < 0:
                chosen = key
                break
    preset = rng.choice(groups[chosen])
    if observer:
        try:
            observer("vst_plugin", keys, weights, chosen)
            observer("vst_preset", groups[chosen], [1]*len(groups[chosen]), preset)
        except Exception: pass  # telemetry cannot affect the chosen sound
    return preset


def verify_loaded_rack(rack, entry):
    if str(getattr(rack, "class_name", "")) != "InstrumentGroupDevice" or not list(getattr(rack, "chains", ())):
        raise ValueError("Rack preset replacement not ready")
    return True
