"""Read-only Browser discovery and exact-family selection for reviewed M4L instruments."""
import re

try:
    from .browser_utils import children_of, is_adv_preset, normalize_name, preset_name_key, safe_attr, walk
    from .device_catalog import M4L_INSTRUMENT_PRESET_EXCLUSIONS
except ImportError:  # standalone policy tests, without importing Live
    from browser_utils import children_of, is_adv_preset, normalize_name, preset_name_key, safe_attr, walk
    from device_catalog import M4L_INSTRUMENT_PRESET_EXCLUSIONS


def instrument_presets(root, supported_names, prune=None):
    """Find presets under exact M4L instrument nodes; never fuzzy-match a Bass category.

    ``prune`` has :func:`browser_utils.walk` semantics: it receives a child's normalized name path
    below ``root`` and returns True to skip that subtree.
    """
    supported = {normalize_name(name): name for name in supported_names}
    found = {name: [] for name in supported_names}
    seen_presets = {name: set() for name in supported_names}
    stack = [(root, False, 0, ())]
    seen = set()
    while stack:
        node, max_parent, depth, path = stack.pop()
        uri = str(safe_attr(node, "uri", "") or "")
        name = str(safe_attr(node, "name", "") or "")
        key = (uri or id(node), name, max_parent)
        if key in seen or depth > 14:
            continue
        seen.add(key)
        stem = normalize_name(re.sub(r"\.amxd$", "", name, flags=re.I))
        max_parent = max_parent or stem == "maxinstrument" or "maxinstrument" in normalize_name(uri)
        canonical = supported.get(stem)
        device_node = (max_parent or bool(safe_attr(node, "is_device", False))
                       or name.lower().endswith(".amxd"))
        if canonical and device_node:
            excluded = {preset_name_key(value) for value
                        in M4L_INSTRUMENT_PRESET_EXCLUSIONS.get(canonical, ())}
            for child in children_of(node):
                for preset, _ in walk(child):
                    if not is_adv_preset(preset):
                        continue
                    if preset_name_key(safe_attr(preset, "name", "")) in excluded:
                        continue
                    marker = str(safe_attr(preset, "uri", "") or "")
                    if not marker or marker in seen_presets[canonical]:
                        continue
                    seen_presets[canonical].add(marker)
                    found[canonical].append(preset)
        for child in reversed(children_of(node)):
            child_path = path + (normalize_name(safe_attr(child, "name", "")),)
            if prune is not None and prune(child_path):
                continue
            stack.append((child, max_parent, depth + 1, child_path))
    return {name: values for name, values in found.items() if values}


def choose_instrument_preset(pools, choice, weights=None, random=None, observer=None):
    """Choose a family first, then its preset. Equal ignores library size; Presets weights each
    family by its startup preset count (``weights``), drawn with ``random``."""
    names = [name for name, presets in pools.items() if presets]
    if not names:
        return None
    total = sum(max(0, weights.get(name, 0)) for name in names) if weights else 0
    if total > 0 and random is not None:
        threshold = random() * total
        name = names[-1]
        for candidate in names:
            threshold -= max(0, weights.get(candidate, 0))
            if threshold < 0:
                name = candidate
                break
    else:
        name = choice(names)
    preset = choice(pools[name])
    if observer:
        try:
            observer("m4l_device", names, [max(0,weights.get(n,0)) for n in names] if total>0 and random is not None else [1]*len(names), name)
            observer("m4l_preset", pools[name], [1]*len(pools[name]), preset)
        except Exception: pass
    return name, preset
