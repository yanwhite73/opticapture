"""Read-only on-disk preset index for the User Library and seven Live Collections.

The loader must know which `.adv` device presets are user presets and which collection colors contain
them, keyed by ``(device, name)``, so it can restrict or augment the candidate pool reliably. The Live
Browser cannot give this reliably (Collection BrowserItem URIs are unstable, so device inference from
them "could bite"). Ableton persists the truth on disk, so we read it directly:

  * **user presets** = the User Library folder tree
    (``User Library/Presets/{Instruments,Audio Effects}/<device>/…*.adv``) — device is the folder.
  * **collection presets** = Ableton ``.xmp`` sidecars under ``Ableton Folder Info/``
    (``ablFR:filePath`` + ``ablFR:colors``); values are one-based ``1..7`` and Orange is ``2``
    (verified by tag-and-diff).
    Device is taken from the ``Instruments``/``Audio Effects`` segment of the filePath.

Matching is deliberately **device-scoped**: a candidate matches only when the preset's path-derived
device equals the generated device. Name-alone matching is refused (a bare name can exist under two
devices — the user flagged this as a broken/misleading filter). Collection presets stored inside packs,
whose path has no device segment, are therefore *not* matched here; resolving those needs the `.adv`
classifier (design.md §8) and is deferred rather than allowed to match by name.

Only `.adv` Device Presets are indexed. `.adg` racks, samples and clips in the tagged set are out of
scope here (design.md §4 taxonomy).

Reads only — never writes. (The Remote Script quarantine forbids file *writes*, not reads.) Every
parse is fail-closed: a bad file contributes nothing rather than raising.

Cross-platform: the companion derives the active User Library from its own installed
``<User Library>/Remote Scripts/GeneratorPresetLoaderV2`` path. ``build_index`` also resolves the
normal macOS/Windows Ableton folder and accepts ``GENERATOR_PRESETS_LIVE_ROOT`` as a development
override.
"""

import os
import re
import sys
import xml.etree.ElementTree as ET

try:  # package import inside Live
    from .browser_utils import normalize_name, preset_name_key
except ImportError:  # flat import in unit tests
    from browser_utils import normalize_name, preset_name_key


COLLECTION_COLOR_IDS = tuple(str(index) for index in range(1, 8))
_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_RDF_LI = "{%s}li" % _RDF
_NS = {"ablFR": "https://ns.ableton.com/xmp/fs-resources/1.0/", "rdf": _RDF}
_CATEGORIES = ("Instruments", "Audio Effects", "Max Instrument")


def device_from_preset_path(filepath):
    """Return the device folder, including Live's nested/direct Max Instrument hierarchy."""
    parts = [p for p in re.split(r"[\\/]+", str(filepath or "")) if p]
    for i, seg in enumerate(parts[:-1]):
        if seg in _CATEGORIES:
            index = i + 1
            if parts[index] == "Max Instrument":
                index += 1
            return parts[index] if index < len(parts) - 1 else None
    return None


def _empty_collection_sets():
    return {color_id: set() for color_id in COLLECTION_COLOR_IDS}


def _segments(path):
    return [p for p in re.split(r"[\\/]+", str(path or "")) if p and p != "."]


# Where Drum Rack kits live. A central/pack sidecar lists pack-relative paths (``Drums/…``,
# ``Racks/Drum Racks/…``); a User Library kit sits under ``Presets/Instruments/Drum Rack``.
# Anything else tagged `.adg` — Instrument Racks, converted VST racks — is not a kit, even inside a
# folder that happens to be called "Drums" (the rack-preset-kit writes category folders so named).
_PACK_KIT_PREFIXES = (("drums",), ("racks", "drumracks"), ("racks", "drumsequences"))
_USER_KIT_PREFIX = ("presets", "instruments", "drumrack")


def is_kit_path(filepath, folder=None):
    """True only for a Drum Rack preset location.

    ``folder`` is the User Library-relative folder a per-folder sidecar describes; its ``filePath``
    values are then bare names. ``None`` means a central/pack sidecar with pack-relative paths.
    """
    if folder is not None:
        parts = [normalize_name(p) for p in _segments(folder) + _segments(filepath)]
        return tuple(parts[:3]) == _USER_KIT_PREFIX
    parts = [normalize_name(p) for p in _segments(filepath)]
    return (tuple(parts[:3]) == _USER_KIT_PREFIX
            or any(tuple(parts[:len(prefix)]) == prefix and len(parts) > len(prefix)
                   for prefix in _PACK_KIT_PREFIXES))


def collection_members_from_xmp_text(text, folder=None):
    """Parse one XMP document once.

    Return ``(preset_pairs, kit_names)`` where each value is keyed by Live's one-based collection
    color ID. Invalid XML contributes two empty maps so callers remain fail-closed.

    ``folder`` is the User Library-relative folder described by a per-folder sidecar
    (``<folder>/Ableton Folder Info/<uuid>.xmp``). Live and the rack-preset-kit both write those
    with bare file names, so the folder supplies the device segment for `.adv` pairs and the
    location check for kits. ``None`` means a central/pack sidecar with pack-relative paths.
    """
    preset_pairs = _empty_collection_sets()
    kit_names = _empty_collection_sets()
    try:
        root = ET.fromstring(text)
    except Exception:
        return preset_pairs, kit_names
    for li in root.iter(_RDF_LI):
        fp_el = li.find("ablFR:filePath", _NS)
        colors_el = li.find("ablFR:colors", _NS)
        if fp_el is None or colors_el is None:
            continue
        filepath = fp_el.text or ""
        low = filepath.lower()
        if not (low.endswith(".adv") or low.endswith(".adg")):
            continue
        colors = {(c.text or "").strip() for c in colors_el.iter(_RDF_LI)}
        selected = colors.intersection(COLLECTION_COLOR_IDS)
        if not selected:
            continue
        if low.endswith(".adv"):
            target = preset_pairs
            located = "/".join(_segments(folder) + [filepath]) if folder is not None else filepath
            value = _preset_pair(located)
        elif is_kit_path(filepath, folder):
            target, value = kit_names, _item_name(filepath)
        else:
            continue
        for color_id in selected:
            target[color_id].add(value)
    return preset_pairs, kit_names


def _preset_pair(filepath):
    device = device_from_preset_path(filepath)
    return (normalize_name(device) if device else None,
            preset_name_key(os.path.basename(filepath.replace("\\", "/"))))


def _item_name(filepath):
    return preset_name_key(os.path.basename(filepath.replace("\\", "/")))


def collection_pairs_from_xmp_text(text):
    """Device-scoped `.adv` membership for every collection color in one XMP document."""
    return collection_members_from_xmp_text(text)[0]


def collection_kit_names_from_xmp_text(text):
    """Drum Rack/rack `.adg` name membership for every collection color in one XMP document."""
    return collection_members_from_xmp_text(text)[1]


def user_pairs_from_relpaths(relpaths):
    """Return ``{(device_key, name_key)}`` from preset paths relative to the ``Presets`` folder, e.g.
    ``"Instruments/Operator/Bass/Deep.adv"``. Pure; non-preset / malformed entries are skipped."""
    out = set()
    for rel in relpaths:
        parts = [p for p in re.split(r"[\\/]+", str(rel or "")) if p]
        if len(parts) < 3 or parts[0] not in _CATEGORIES:
            continue
        if not parts[-1].lower().endswith(".adv"):
            continue
        device = device_from_preset_path(rel)
        if device:
            out.add((normalize_name(device), preset_name_key(parts[-1])))
    return out


class PresetIndex:
    """Device-scoped User Library and per-collection preset membership."""

    def __init__(self, user=None, collections=None, user_kits=None, collection_kits=None,
                 adv_folders=None):
        self.user = set(user or ())
        self.collections = _empty_collection_sets()
        self.collection_kits = _empty_collection_sets()
        for color_id, pairs in (collections or {}).items():
            if str(color_id) in self.collections:
                self.collections[str(color_id)].update(pairs)
        for color_id, names in (collection_kits or {}).items():
            if str(color_id) in self.collection_kits:
                self.collection_kits[str(color_id)].update(names)
        self.user_kits = set(user_kits or ())
        # {"Instruments"|"Audio Effects": {normalized top-level folder containing any .adv}}, or
        # None when this index cannot vouch for the library Live is showing (no Browser pruning).
        self.adv_folders = adv_folders

    @staticmethod
    def _collection_ids(collection_ids):
        selected = {str(value) for value in (collection_ids or ())}
        return tuple(color_id for color_id in COLLECTION_COLOR_IDS if color_id in selected)

    def kit_names(self, use_user, collection_ids=()):
        """Name-keys of drum kits from the union of enabled personal sources."""
        names = set()
        if use_user:
            names |= self.user_kits
        for color_id in self._collection_ids(collection_ids):
            names |= self.collection_kits[color_id]
        return names

    def source_pairs(self, use_user, collection_ids=()):
        """Device/preset pairs from the union of enabled personal sources."""
        pairs = set()
        if use_user:
            pairs |= self.user
        for color_id in self._collection_ids(collection_ids):
            pairs |= self.collections[color_id]
        return pairs

    def device_has_source(self, device_key, use_user, collection_ids=()):
        """True if any enabled-source preset belongs to ``device_key`` — drives 'only'-mode skipping."""
        return any(d == device_key for d, _n in self.source_pairs(use_user, collection_ids))

    def matches(self, device_key, name_key, use_user, collection_ids=()):
        """True if ``(device_key, name_key)`` is an enabled-source preset. Device-scoped: a ``None``
        device pair never matches (pack-organised collection membership is deferred)."""
        if device_key is None:
            return False
        return (device_key, name_key) in self.source_pairs(use_user, collection_ids)

    def counts(self):
        return {
            "user": len(self.user),
            "collections": {color_id: len(self.collections[color_id])
                            for color_id in COLLECTION_COLOR_IDS},
            "user_kits": len(self.user_kits),
            "collection_kits": {color_id: len(self.collection_kits[color_id])
                                for color_id in COLLECTION_COLOR_IDS},
        }


LIVE_ROOT_ENV = "GENERATOR_PRESETS_LIVE_ROOT"


def installed_user_library(package_file=None, path_module=None):
    """Return the User Library containing an installed companion package, or ``None``.

    This is deliberately structural rather than platform-specific: on both macOS and Windows the
    package is installed directly under ``<User Library>/Remote Scripts``. Source checkouts and
    malformed/nested installations fail closed to the normal platform fallback.
    """
    paths = path_module or os.path
    source = paths.abspath(str(package_file or __file__))
    package_dir = paths.dirname(source)
    remote_scripts_dir = paths.dirname(package_dir)
    if paths.basename(package_dir).casefold() != "generatorpresetloaderv2":
        return None
    if paths.basename(remote_scripts_dir).casefold() != "remote scripts":
        return None
    return paths.dirname(remote_scripts_dir)


def live_root_candidates(home=None, platform=None, environ=None):
    env = os.environ if environ is None else environ
    override = str(env.get(LIVE_ROOT_ENV, "") or "").strip()
    if override:
        return [os.path.abspath(os.path.expanduser(override))]
    root = os.path.expanduser("~") if home is None else os.path.abspath(str(home))
    current_platform = sys.platform if platform is None else str(platform)
    parent = "Documents" if current_platform.startswith("win") else "Music"
    return [os.path.join(root, parent, "Ableton")]


def _default_live_root():
    candidates = live_root_candidates()
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return candidates[0]


def iter_preset_paths(user_library, categories=_CATEGORIES):
    """List existing presets without opening their bodies; shared by native/Rack discovery."""
    base = os.path.join(str(user_library), "Presets")
    def failed(error):
        raise error
    for category in categories:
        folder = os.path.join(base, category)
        if not os.path.isdir(folder):
            continue
        for dirpath, dirs, files in os.walk(folder, onerror=failed, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d != "Ableton Folder Info"
                             and not os.path.islink(os.path.join(dirpath, d)))
            yield None  # let incremental callers yield between folders
            for name in sorted(files):
                if name.lower().endswith((".adv", ".adg")):
                    yield "Presets/" + os.path.relpath(os.path.join(dirpath, name), base).replace(os.sep, "/")


def build_index(live_root=None, user_library=None):
    """Walk the Ableton library **read-only** and return a :class:`PresetIndex`.

    Fail-closed: any error while walking leaves that source empty rather than raising, so a preset
    filter can never crash a roll. Intended to be built once and cached by the caller — a full walk is
    not cheap on a large library and must not run per roll on Live's thread. ``user_library`` is the
    exact folder containing ``Presets`` and takes precedence over ``live_root``.
    """
    exact_library = user_library is not None
    if user_library is None:
        if live_root is None:
            live_root = _default_live_root()
        user_library = os.path.join(live_root, "User Library")
        metadata_root = live_root
    else:
        user_library = os.path.abspath(str(user_library))
        metadata_root = user_library
    user = set()
    collections = _empty_collection_sets()
    user_kits = set()
    collection_kits = _empty_collection_sets()
    adv_folders = {"Instruments": set(), "Audio Effects": set()}
    walk_complete = True

    presets_base = os.path.join(user_library, "Presets")
    for category in _CATEGORIES:
        base = os.path.join(presets_base, category)
        if not os.path.isdir(base):
            continue
        try:
            for relative in iter_preset_paths(user_library, (category,)):
                if relative is not None and relative.lower().endswith(".adv"):
                    rel = relative[len("Presets/"):]
                    user |= user_pairs_from_relpaths([rel])
                    parts = _segments(rel)
                    if category in adv_folders and len(parts) >= 3:
                        adv_folders[category].add(normalize_name(parts[1]))
        except Exception:
            walk_complete = False

    # User drum kits: `.adg` under Presets/Instruments/Drum Rack (so `user` applies to kits too).
    drum_dir = os.path.join(presets_base, "Instruments", "Drum Rack")
    if os.path.isdir(drum_dir):
        try:
            for dirpath, _dirs, files in os.walk(drum_dir):
                for name in files:
                    if name.lower().endswith(".adg"):
                        user_kits.add(preset_name_key(name))
        except Exception:
            pass

    # Collection membership lives in `.xmp` sidecars. On Live 12 they are NOT only in dirs named
    # "Ableton Folder Info" — the central store is `User Library/Ableton Folder Info/12/<uuid>.xmp`,
    # one level deeper. So find every `.xmp` under the library rather than gating on the parent name
    # (gating on "Ableton Folder Info" silently missed ~half the collection data — verified).
    try:
        for dirpath, _dirs, files in os.walk(metadata_root):
            for name in files:
                if not name.lower().endswith(".xmp"):
                    continue
                try:
                    with open(os.path.join(dirpath, name), "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except Exception:
                    continue
                folder = None
                if os.path.basename(dirpath) == "Ableton Folder Info":
                    described = os.path.dirname(dirpath)
                    try:
                        folder = os.path.relpath(described, user_library)
                    except ValueError:  # different drive on Windows
                        folder = None
                    if folder is not None and folder.startswith(".."):
                        folder = None
                pairs_by_color, kits_by_color = collection_members_from_xmp_text(text, folder)
                for color_id in COLLECTION_COLOR_IDS:
                    collections[color_id] |= pairs_by_color[color_id]
                    collection_kits[color_id] |= kits_by_color[color_id]
    except Exception:
        pass

    # Browser pruning is only safe when this is provably the library Live shows (the companion's
    # own install location) and the walk finished; otherwise report "unknown" and prune nothing.
    return PresetIndex(user=user, collections=collections, user_kits=user_kits,
                       collection_kits=collection_kits,
                       adv_folders=adv_folders if exact_library and walk_complete else None)
