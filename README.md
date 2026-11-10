# opticapture

A separate Max for Live Audio Effect for keeping a recorded loop together with
the setup that made it.

- **Keep Take** names the selected audio clip and its file together and retains
  a full native source Set.
- **Keep Scene** keeps the audio clips in the selected Session scene.
- **Find Source** reveals the linked source Set in Finder for reworking.

The device passes audio through unchanged. Put it on an audio track or after an
instrument, select the audio clip or scene, and check the displayed target before
clicking Keep. Record, stop, and Keep before changing the source setup. For older
recordings, **Use current setup** explicitly associates the setup loaded now; it
cannot reconstruct an earlier state.

## Development status

This is the capture source from Optimystery **2.0.0-alpha.20-dev.51**, including
dev.50's prior-view restoration, scoped native UI searches and stage timings.
It uses the existing Optimystery companion and capture-state export. The main
Optimystery device is maintained in
[ableton-randomizer](https://github.com/yanwhite73/ableton-randomizer).

Native capture and matching clip/file naming have been user-confirmed. Actual
prior-view restoration, total capture latency, source sound/state after reopening,
and Keep Scene acceptance remain open. Offline tests do not establish those Live
results. This is a development snapshot, not a stable release.

## Requirements and installation layout

- Ableton Live with Max for Live, on Apple silicon macOS 13 or later.
- English Live UI for the native Browser/Save helper.
- Python 3.9 or later (`/usr/bin/python3` for the device buttons) and the matching
  Optimystery companion; compatible source
  Optimystery instances need the capture-state support introduced in dev.43.
- macOS Accessibility permission for the calling app: Live for device buttons,
  Terminal for command-line use.

The original `generator_presets_v2/` layout is retained so the existing build
tools and tests run without path changes. Install from these source locations:

| Source | Location inside your Ableton User Library |
| --- | --- |
| `generator_presets_v2/capture/device/` | `Max Audio Effect/opticapture/` |
| `generator_presets_v2/remote_script/GeneratorPresetLoaderV2/` | `Remote Scripts/GeneratorPresetLoaderV2/` |
| `capture_core.py`, `optimystery_capture.py`, `native.swift`, `capture-native` from `generator_presets_v2/capture/` | `Optimystery Capture/App/` |
| The three `.command` files, `native-build.json` and helper `README.md` from that same directory | `Optimystery Capture/` |

Back up an existing installation outside Live/Max search paths and preserve its
JSON settings, capture index, Bridge, Logs and history. The companion is shared
with Optimystery; install the full matching folder, not only `take_capture.py`.
Select **GeneratorPresetLoaderV2** as a Control Surface in Live, with no MIDI
input/output ports. Restart Live once after changing companion files. Changed
device JS/patch files require a fresh opticapture instance; helper updates take
effect on the next button click. Keep the `.command` files and `capture-native`
executable. The tracked native binary targets Apple silicon macOS 13+.

The capture snapshot remains beside the working Set. `Optimystery Takes/<id>/`
contains a manifest and audio backups; source lookup also uses a local index.
The old audio path is retained for other Sets. This does not collect external
samples or install missing plug-ins, and it does not reproduce random LFO phase.
Keep the recorded audio as the performance reference. Open a source Set yourself
and use Save As before reworking it.

## Build and test

Python uses the standard library. The device uses Max's bundled Node for Max;
no npm dependencies are required. Building the native helper requires Apple's
Swift compiler. Building/testing the AMXD requires Live's own Max Audio Effect
template; that template is not redistributed here.

From `generator_presets_v2/`:

```sh
python3 capture/build_native.py
python3 build_capture.py
python3 -m unittest discover -s tests -p 'test_*.py'
node tests/test_capture_device.js
```

`build_capture.py` preserves the real template's audio graph and container. It
also emits an inspectable `.maxpat` and a Max-side builder for use through Live's
integrated Max editor. Native loading remains a separate check. Follow any local
workload-management policy when running builds/tests.

## Components and controls

The Max UI sends explicit actions to Node, which starts the Python transaction
helper. Python uses the companion's local JSON mailbox for Live state and a
first-party Swift Accessibility helper for native Save and Browser rename. The
capture path opens no network sockets or MIDI ports. It writes capture metadata,
source snapshots, requested clip/file names, backups and its local lookup index.
Details and `take.json` retain errors and stage/operation timings.

The included companion also serves Optimystery's existing device/preset loading
and history functions; it is not a capture-only Remote Script. Removing the
opticapture device/helper folders removes the front end and commands; deselecting
the companion stops its recording observation/mailbox and its Optimystery services.
Retain captured source Sets, take folders and the lookup index for recall.
