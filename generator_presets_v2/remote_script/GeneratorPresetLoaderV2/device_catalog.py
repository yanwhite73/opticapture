"""Validated, declarative Optimystery device policy.

The JSON beside this module is the single source of truth for supported native instruments,
sample hosts, native effects, and explicitly allowlisted first-party M4L effects. Runtime Browser
discovery may remove unavailable entries; it never expands this curated catalog.
"""

import json
import math
import os


CATALOG_FILENAME = "device_catalog.v1.json"
CATALOG_SCHEMA_VERSION = 1
# "dynamics" (Compressor, Limiter) has its own Gran dial since 2026-09-26.
EFFECT_FAMILIES = frozenset(("modulation", "drive", "filter", "pitch", "delay", "reverb", "dynamics"))
# Availability travels as Float parameters (Int is byte-sized in Live). Each carries at most 20
# bits, well inside a float's exact-integer range (2**24). The number of channels is derived from
# the catalog, so the catalog can grow without a protocol cap (see mask_channels).
MASK_CHANNEL_WIDTH = 20


def _names(entries):
    return tuple(entry["name"] for entry in entries)


def load_catalog(path=None):
    source = path or os.path.join(os.path.dirname(__file__), CATALOG_FILENAME)
    with open(source, "r", encoding="utf-8") as handle:
        catalog = json.load(handle)
    if catalog.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ValueError("unsupported device catalog schema")

    sections = ("native_instruments", "sample_hosts", "native_effects", "m4l_effects", "m4l_instruments")
    all_names = []
    for section in sections:
        entries = catalog.get(section)
        if not isinstance(entries, list) or not entries:
            raise ValueError("device catalog section %s must be a non-empty list" % section)
        for entry in entries:
            if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
                raise ValueError("device catalog section %s contains an invalid entry" % section)
            all_names.append(entry["name"])
            if section.endswith("effects") and entry.get("family") not in EFFECT_FAMILIES:
                raise ValueError("device %s has an invalid effect family" % entry["name"])
            excluded = entry.get("excluded_presets", [])
            if not isinstance(excluded, list) or any(not isinstance(name, str) or not name.strip()
                                                     for name in excluded):
                raise ValueError("device %s has invalid preset exclusions" % entry["name"])
    if len(all_names) != len(set(all_names)):
        raise ValueError("device catalog names must be unique")

    drum_source = catalog.get("drum_source")
    if not isinstance(drum_source, dict) or not str(drum_source.get("name", "")).strip():
        raise ValueError("device catalog drum_source is invalid")
    return catalog


CATALOG = load_catalog()
DRUM_SOURCE_NAME = CATALOG["drum_source"]["name"]
SYNTH_INSTRUMENT_NAMES = _names(CATALOG["native_instruments"])
SAMPLE_HOST_NAMES = _names(CATALOG["sample_hosts"])
NATIVE_EFFECT_NAMES = _names(CATALOG["native_effects"])
M4L_EFFECT_NAMES = _names(CATALOG["m4l_effects"])
M4L_INSTRUMENT_NAMES = _names(CATALOG["m4l_instruments"])
M4L_INSTRUMENT_PRESET_EXCLUSIONS = {
    entry["name"]: tuple(entry.get("excluded_presets", ())) for entry in CATALOG["m4l_instruments"]
}
CONTENT_REQUIRED_M4L_EFFECT_NAMES = tuple(
    entry["name"] for entry in CATALOG["m4l_effects"] if entry.get("requires_preset")
)
EFFECT_FAMILY_BY_NAME = {
    entry["name"]: entry["family"]
    for section in ("native_effects", "m4l_effects")
    for entry in CATALOG[section]
}


def names_to_mask(ordered_names, available_names):
    """Encode an availability set using the stable catalog order."""
    available = set(available_names or ())
    return sum(1 << index for index, name in enumerate(ordered_names) if name in available)


def mask_channels(width=MASK_CHANNEL_WIDTH):
    """Every availability channel as ``(key, parameter longname, ordered names)``.

    Each catalog list is split into as many ``width``-bit channels as its length needs, so the
    device holders, companion publisher and device decoder all follow the catalog's size.
    """
    channels = []
    for kind, label, names in (("instruments", "Instruments", SYNTH_INSTRUMENT_NAMES),
                               ("native", "Native", NATIVE_EFFECT_NAMES),
                               ("m4l", "M4L", M4L_EFFECT_NAMES)):
        for index in range(max(1, int(math.ceil(len(names) / float(width))))):
            channels.append(("%s_%d" % (kind, index),
                             "Availability %s %d V3" % (label, index + 1),
                             names[index * width:(index + 1) * width]))
    return tuple(channels)


AVAILABILITY_CHANNELS = mask_channels()
# Content bits: Drum Rack kits, one per sample host, then M4L instrument presets.
CONTENT_MASK_WIDTH = 2 + len(SAMPLE_HOST_NAMES)
AVAILABILITY_CONTENT_PARAMETER = "Availability Content V3"
AVAILABILITY_REVISION_PARAMETER = "Availability Revision V3"
