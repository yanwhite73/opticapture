"""Sound types from existing folder metadata, never from preset names or contents.

This table also generates the Max constants and the native parameter contract.
IDs are stable for external controllers (pad means melodic sound, not Drum Pads).
"""
import re
from functools import lru_cache
from urllib.parse import unquote

SOUND_TYPES = (
    ("bass", "Bass", ("bass", "basses", "synth bass")),
    ("pad", "Pad", ("pad", "pads", "synth pad")),
    ("sequence", "Sequence", ("sequence", "sequences", "sequenced", "seq", "arpeggio", "arpeggios", "arp", "arps", "rhythmic", "synth rhythmic")),
    ("lead", "Lead", ("lead", "leads", "synth lead", "synth leads")),
    ("keys", "Keys", ("keys", "keys a", "keys b", "keyboard", "keyboards", "synth keys", "poly", "polysynth", "polysynths", "piano & keys")),
    ("piano", "Piano", ("piano", "pianos", "acoustic piano")),
    ("electric_piano", "Electric Piano", ("electric piano", "electric pianos", "e-piano", "epiano")),
    ("organ", "Organ", ("organ", "organs")),
    ("strings", "Strings", ("string", "strings", "bowed strings")),
    ("brass_winds", "Brass & Winds", ("brass & winds", "brass", "wind", "winds", "woodwind", "woodwinds")),
    ("plucked", "Plucked", ("pluck", "plucks", "plucked", "guitar & plucked", "plucked strings", "guitar", "guitars")),
    ("mallets", "Mallets", ("mallet", "mallets", "bell", "bells")),
    ("drums", "Drums / Percussion", ("drum", "drums", "percussion", "percussive", "hits")),
    ("vocal", "Vocal", ("vocal", "vocals", "voice", "voices", "choir")),
    ("effects", "Sound Effects", ("sound effects", "sfx", "fx", "effects", "noise", "noises", "atmos-fx")),
    ("atmosphere", "Atmosphere", ("atmosphere", "atmospheres", "ambient", "ambient & evolving", "soundscape", "soundscapes", "texture", "textures")),
    ("chords", "Chords", ("chord", "chords")),
    ("loops", "Loops", ("loop", "loops")),
    ("template", "Template", ("template", "templates", "init", "initial")),
    ("uncategorised", "Uncategorised", ()),
)
TYPE_IDS = tuple(row[0] for row in SOUND_TYPES)


def normalize_folder(value):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"^\d+[\s._-]+", "", value.strip().lower()))


ALIASES = {normalize_folder(alias): key for key, _label, aliases in SOUND_TYPES for alias in aliases}


def folder_category(parts):
    return next((ALIASES[normalize_folder(part)] for part in parts if normalize_folder(part) in ALIASES), "uncategorised")


@lru_cache(maxsize=32768)
def vst_category(path):
    return folder_category(str(path).split("/")[4:-1])


@lru_cache(maxsize=32768)
def native_category(uri):
    if "#" not in uri:
        return "uncategorised"
    # Split BEFORE decoding: an escaped colon/slash belongs to a folder name.
    parts = [unquote(part) for part in uri.split("#", 1)[1].split(":")]
    start = 3 if parts[:2] == ["Presets", "Instruments"] else 1
    return folder_category(parts[start:-1])


def category_selection(read_parameter):
    selected = frozenset(key for key in TYPE_IDS if read_parameter("Sound Type " + key, 1) >= 0.5)
    solo = read_parameter("Pool Type Solo", 0)
    if solo:
        if solo != int(solo) or not 1 <= solo <= len(TYPE_IDS):
            return frozenset()
        return selected & frozenset((TYPE_IDS[int(solo) - 1],))
    return None if len(selected) == len(TYPE_IDS) else selected


def filter_native(items, categories):
    if categories is None:
        return items
    return [item for item in items if native_category(str(getattr(item, "uri", "") or "")) in categories]


def filter_vst(entries, categories):
    if categories is None:
        return entries
    return [entry for entry in entries if vst_category(entry["path"]) in categories]
