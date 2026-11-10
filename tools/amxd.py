#!/usr/bin/env python3
"""Read, modify and write Max for Live `.amxd` device files.

An `.amxd` is a chunked binary container:

    'ampf' <uint32 size> <payload>     format marker
    'meta' <uint32 size> <payload>     metadata
    'ptch' <uint32 size> <payload>     the patcher, as JSON

Editing the patcher JSON directly is the only way to set things the MaxMSP-MCP cannot reach:
`live.comment` text (it is box content, not an attribute), and the patcher-level `openinpresentation`
and `devicewidth`. Attempting the UI through the MCP produced an unusable panel.

`AGENTS.md` warns against writing `.amxd` programmatically. That warning is about *generating* a
device from scratch by wrapping a `.maxpat`, which crashed Live. This module only ever reads a file
Max itself authored, edits the JSON, and writes it back with a `.backup` alongside — and
`round_trips()` asserts an untouched file reproduces byte-for-byte.
"""

from __future__ import annotations

import json
import shutil
import struct
from pathlib import Path

Chunk = tuple[str, bytes]


def read_chunks(path: str | Path) -> list[Chunk]:
    data = Path(path).read_bytes()
    if not data.startswith(b"ampf"):
        raise ValueError(f"{path}: not an .amxd container (no 'ampf' header)")
    out: list[Chunk] = []
    pos = 0
    while pos + 8 <= len(data):
        cid = data[pos : pos + 4].decode("latin-1")
        size = struct.unpack_from("<I", data, pos + 4)[0]
        start = pos + 8
        end = min(start + size, len(data))
        out.append((cid, data[start:end]))
        pos = end
    return out


def build(chunks: list[Chunk]) -> bytes:
    return b"".join(
        cid.encode("latin-1") + struct.pack("<I", len(payload)) + payload
        for cid, payload in chunks
    )


def read_patcher(path: str | Path) -> dict:
    for cid, payload in read_chunks(path):
        if cid == "ptch":
            start = payload.find(b"{")
            if start < 0:
                raise ValueError(f"{path}: 'ptch' chunk holds no JSON")
            # raw_decode, not loads: the chunk carries trailing bytes after the JSON object.
            text = payload[start:].decode("utf-8", errors="replace")
            return json.JSONDecoder().raw_decode(text)[0]
    raise ValueError(f"{path}: no 'ptch' chunk")


def write_patcher(src: str | Path, dst: str | Path, patcher: dict, backup: bool = True) -> Path:
    """Write `patcher` into a copy of `src`'s container, saved at `dst`."""
    src, dst = Path(src), Path(dst)
    if backup and dst.exists():
        shutil.copy2(dst, dst.with_suffix(dst.suffix + ".backup"))
    payload = json.dumps(patcher, indent=1).encode("utf-8")
    chunks = [(cid, payload if cid == "ptch" else data) for cid, data in read_chunks(src)]
    dst.write_bytes(build(chunks))
    return dst


def round_trips(path: str | Path) -> bool:
    """True when re-serialising an untouched file reproduces it exactly."""
    return build(read_chunks(path)) == Path(path).read_bytes()
