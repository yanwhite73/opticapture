"""Pure helpers for indexing Live BrowserItem trees.

This module deliberately imports no Ableton modules, so its selection rules can be unit-tested
outside Live. BrowserItem itself is an undocumented Remote Script API in current Live builds; all
attribute reads are therefore defensive.
"""

import re
from urllib.parse import unquote


def normalize_name(value):
    """Return a punctuation-insensitive name used only for native-device matching."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def preset_name_key(basename):
    """Normalize a Browser/disk preset name after stripping its Live preset extension."""
    stem = re.sub(r"\.(adv|adg)$", "", str(basename or ""), flags=re.IGNORECASE)
    return normalize_name(stem)


# Presets and kits Yan never wants offered (2026-09-25: "Moscow", e.g. the Resonators preset and
# the Moscow UHF drum kit). Matched as whole words in the Browser name; Ableton's own files are
# left untouched.
EXCLUDED_NAME_WORDS = ("moscow",)


def is_excluded_name(name):
    words = re.findall(r"[a-z0-9]+", re.sub(r"\.(adv|adg|amxd)$", "", str(name or ""), flags=re.I).lower())
    return any(word in EXCLUDED_NAME_WORDS for word in words)


def without_excluded(items):
    """Drop Browser items whose display name contains an excluded word."""
    return [item for item in items if not is_excluded_name(safe_attr(item, "name", ""))]


def safe_attr(item, name, default=None):
    try:
        return getattr(item, name)
    except Exception:
        return default


def children_of(item):
    children = safe_attr(item, "children", ())
    try:
        return tuple(children or ())
    except Exception:
        return ()


def walk(root, max_depth=14, prune=None):
    """Yield ``(item, depth)`` without trusting BrowserItem identity or unlimited recursion.

    ``prune`` optionally receives the normalized name path of a child below ``root`` and returns
    True to skip that child and its subtree. See :func:`adv_folder_prune`.
    """
    stack = [(root, 0, ())]
    seen = set()
    while stack:
        item, depth, path = stack.pop()
        marker = safe_attr(item, "uri", None)
        if not marker:
            marker = id(item)
        marker = (str(marker), normalize_name(safe_attr(item, "name", "")))
        if marker in seen:
            continue
        seen.add(marker)
        yield item, depth
        if depth >= max_depth:
            continue
        kids = children_of(item)
        for child in reversed(kids):
            child_path = path + (normalize_name(safe_attr(child, "name", "")),)
            if prune is not None and prune(child_path):
                continue
            stack.append((child, depth + 1, child_path))


def adv_folder_prune(adv_folders, prefix=()):
    """Skip top-level preset folders that the on-disk index proved hold no ``.adv``.

    Only ``.adv`` Device Presets are ever candidates, so a folder of third-party Instrument Rack
    ``.adg`` files (for example a converted VST library of many thousands) cannot contribute; it
    only costs startup time on Live's thread. ``adv_folders`` is a set of normalized folder names
    directly below ``prefix``; ``None`` means the index cannot vouch for this library, so nothing
    is pruned and behaviour is unchanged.
    """
    if adv_folders is None:
        return None
    prefix = tuple(prefix)
    depth = len(prefix) + 1

    def prune(path):
        return len(path) == depth and path[:-1] == prefix and path[-1] not in adv_folders
    return prune


def _matching_device_nodes(root, aliases):
    wanted = {normalize_name(alias) for alias in aliases if normalize_name(alias)}
    exact = []
    fuzzy = []
    for item, _depth in walk(root):
        name = normalize_name(safe_attr(item, "name", ""))
        if not name:
            continue
        if name in wanted:
            exact.append(item)
        elif any(len(alias) >= 5 and (alias in name or name in alias) for alias in wanted):
            fuzzy.append(item)
    return exact or fuzzy


def available_supported_names(root, supported_names):
    """Return exact supported names represented by a Browser device/group node.

    This is availability discovery, not catalog discovery: names absent from ``supported_names`` are
    never returned. A candidate must be a device, loadable item, or non-empty group so a coincidental
    empty label cannot enable an uninsertable source.
    """
    supported = {normalize_name(name): name for name in supported_names if normalize_name(name)}
    found = set()
    for item, _depth in walk(root):
        raw_name = re.sub(
            r"\.(?:amxd|adv|adg)$", "", str(safe_attr(item, "name", "")), flags=re.IGNORECASE
        )
        canonical = supported.get(normalize_name(raw_name))
        if canonical is None:
            continue
        if (bool(safe_attr(item, "is_device", False))
                or bool(safe_attr(item, "is_loadable", False))
                or bool(children_of(item))):
            found.add(canonical)
    return frozenset(found)


def _child_named(root, wanted):
    wanted_key = normalize_name(wanted)
    for item in children_of(root):
        if normalize_name(safe_attr(item, "name", "")) == wanted_key:
            return item
    return None


def is_adv_preset(item):
    """True only for a loadable Live Device Preset leaf, never a bare device or Rack."""
    if not bool(safe_attr(item, "is_loadable", False)):
        return False
    if bool(safe_attr(item, "is_device", False)):
        return False
    uri = str(safe_attr(item, "uri", ""))
    name = str(safe_attr(item, "name", ""))
    return ".adv" in (uri + " " + name).lower()


# M4L-ness is an inherent, readable property (no manual tagging): a Max for Live device is an `.amxd`,
# and in the Browser it sits under / is marked by the "Max Audio Effect" (or Instrument/MIDI) grouping.
# These normalized substrings identify a Browser item as an M4L AUDIO effect from its uri/name. Kept as
# a tunable set because the exact Browser uri shape is only verifiable live (like the drum-kit markers).
_M4L_AUDIO_MARKERS = ("maxaudioeffect", "maxforlive", "maxdevice", ".amxd")


def is_m4l_audio_effect(uri, name):
    """True if a Browser item looks like a Max for Live AUDIO effect (device or its preset). Pure: takes
    the already-read uri/name so it unit-tests without Live. Excludes M4L instruments/MIDI effects."""
    blob = normalize_name(str(uri or "") + " " + str(name or ""))
    if "maxinstrument" in blob or "maxmidieffect" in blob:
        return False  # an M4L instrument / MIDI effect, not an audio effect
    return any(marker.replace(".", "") in blob for marker in _M4L_AUDIO_MARKERS)


def allowlisted_device_folder(uri, allowed_names):
    """True when the Browser item sits in an allowlisted device's own folder.

    Live lists pack Max effects under Audio Effects as ``query:AudioFx#PitchLoop89`` (the device)
    and ``query:AudioFx#PitchLoop89:...`` (its presets), with no .amxd/Max marker. The first path
    segment after ``#`` is the device folder, so a native preset that merely shares a name
    (``query:AudioFx#Delay:Gated Delay.adv``) is not taken for the Max device.
    """
    text = str(uri or "")
    if "#" not in text:
        return False
    folder = unquote(text.split("#", 1)[1].split(":", 1)[0])
    allowed = {normalize_name(value) for value in allowed_names if normalize_name(value)}
    return normalize_name(re.sub(r"\.(?:amxd|adv)$", "", folder, flags=re.I)) in allowed


def matches_allowlisted_device(uri, name, allowed_names):
    """Match a bare Browser device against an explicit name allowlist, failing closed.

    Browser URIs use several separators across Live versions, so compare normalized *segments* and
    the display name rather than a whole-URI substring. A third-party name that merely contains an
    allowed name is rejected.
    """
    allowed = {normalize_name(value) for value in allowed_names if normalize_name(value)}
    if not allowed:
        return False
    candidates = {normalize_name(re.sub(r"\.(?:amxd|adv)$", "", str(name or ""), flags=re.I))}
    for segment in re.split(r"[:#\\/]", str(uri or "")):
        # Live percent-encodes Browser URI segments (query:AudioFx#Gated%20Delay:...).
        candidates.add(normalize_name(re.sub(r"\.(?:amxd|adv)$", "", unquote(segment), flags=re.I)))
    return bool(allowed.intersection(candidates))


def item_uri(item):
    """Return a stable Browser identity or ``None``; display-name fallback is unsafe."""
    uri = str(safe_attr(item, "uri", "") or "")
    return uri if uri else None


def preset_candidates(root, aliases):
    """Find loadable preset leaves below the best matching native-device Browser node.

    A bare device item is intentionally excluded even if it is loadable. This experiment wants a
    preset replacement, not a second insertion of the factory-default device. Only ``.adv`` Live
    Device Presets are admitted: ``.adg`` racks can replace the generated device with a different
    chain shape and can contain additional devices.
    """
    candidates = []
    seen = set()
    for device_node in _matching_device_nodes(root, aliases):
        for child in children_of(device_node):
            for item, _depth in walk(child):
                if not is_adv_preset(item):
                    continue
                uri = str(safe_attr(item, "uri", ""))
                name = str(safe_attr(item, "name", ""))
                marker = uri or name
                if not marker or marker in seen:
                    continue
                seen.add(marker)
                candidates.append(item)
    return candidates


def user_preset_candidates(user_root, kind, aliases):
    """Find compatible presets only in User Library's standard Presets hierarchy.

    This deliberately does not search arbitrary user folders named like a Live device. If Live's
    standard hierarchy is unavailable, the User filter fails closed.
    """
    if user_root is None:
        return []
    presets = _child_named(user_root, "Presets")
    category = _child_named(
        presets, "Instruments" if kind == "instrument" else "Audio Effects"
    ) if presets is not None else None
    return preset_candidates(category, aliases) if category is not None else []


def catalog_preset_index(root, names, prune=None):
    """Snapshot a whole native catalog at startup, sharing one device-node traversal."""
    nodes = [(item, normalize_name(safe_attr(item, "name", ""))) for item, _ in walk(root, prune=prune)]
    result = {}
    for name in names:
        key = normalize_name(name)
        matches = [item for item, label in nodes if label == key]
        if not matches and len(key) >= 5:
            matches = [item for item, label in nodes if label and (key in label or label in key)]
        result[name] = _unique_items([
            item for node in matches for child in children_of(node) for item, _ in walk(child)
            if is_adv_preset(item)
        ])
    return result


def user_catalog_preset_index(user_root, kind, names, adv_folders=None):
    presets = _child_named(user_root, "Presets")
    category = _child_named(presets, "Instruments" if kind == "instrument" else "Audio Effects")
    return catalog_preset_index(category, names, prune=adv_folder_prune(adv_folders))


def source_matched_candidates(
    factory, user_items, canonical_name, use_user, collection_ids, preset_index
):
    """Return exact-device candidates admitted by User Library and selected Collections.

    User items are trusted by their User Library location. A Collection may include factory or user
    items, so membership is checked by ``(device, preset name)`` against the on-disk index. Synth,
    sample-host, and audio-effect paths share this helper so a new source cannot bypass filtering.
    """
    matched = []
    if use_user:
        matched.extend(user_items)
    if collection_ids and preset_index is not None:
        device_key = normalize_name(canonical_name)
        for item in list(factory) + list(user_items):
            if preset_index.matches(
                device_key,
                preset_name_key(safe_attr(item, "name", "")),
                use_user=False,
                collection_ids=collection_ids,
            ):
                matched.append(item)
    return _unique_items(matched)


def _unique_items(items):
    """Preserve Browser order while de-duplicating a union of source pools."""
    out = []
    seen = set()
    for item in items:
        marker = str(safe_attr(item, "uri", "") or "") or str(id(item))
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


def all_user_presets(user_root, kind, adv_folders=None):
    """Every `.adv` under User Library's ``Presets/{Instruments|Audio Effects}`` — i.e. all your saved
    presets of a kind, across every device. Used to fill ``only`` mode from your whole set when the
    generated device is one you have no preset for. Fails closed to ``[]``.
    """
    if user_root is None:
        return []
    presets = _child_named(user_root, "Presets")
    category = _child_named(
        presets, "Instruments" if kind == "instrument" else "Audio Effects"
    ) if presets is not None else None
    if category is None:
        return []
    out = []
    seen = set()
    for item, _depth in walk(category, prune=adv_folder_prune(adv_folders)):
        if not is_adv_preset(item):
            continue
        marker = str(safe_attr(item, "uri", "") or "") or str(id(item))
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


def item_description(item):
    name = str(safe_attr(item, "name", "<unnamed>"))
    uri = str(safe_attr(item, "uri", ""))
    return "%s (%s)" % (name, uri) if uri else name
