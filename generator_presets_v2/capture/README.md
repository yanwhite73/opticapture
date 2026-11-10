# Keep Take, Keep Scene, Find Source

For visible buttons inside Live, use the separate **opticapture.amxd**
under **User Library → Max Audio Effect → opticapture**. The helper folder
here must remain directly inside User Library. Keep Take, Keep Scene and Find
Source run from that device without opening Terminal; its Details button opens
the full result/error. See the device folder's README. Restart once for the
dev.44 companion; existing dev.43 generator instances remain compatible.

Dev.50 restores prior Detail/Browser views and clip after capture with the updated
companion (one Live restart). Searches avoid unrelated rows; Details and take.json
record stage/operation timings. Old companion still captures but cannot restore
the view. Native performance/restoration remains user verification.

Dev.49 names current-setup captures from the routed source Rack, including nested
Optimystery. File selection reads the sample AXValue inside Clip View, excluding
duplicate Browser items. Clips keep their existing names until all file renames
and Live references verify. Helper-only update: no reload/restart; next click.
Native rename/recall acceptance remains open.

Dev.48 waits through Live's temporary Set-file removal/replacement during Save,
including partial gzip/XML writes, before retaining the complete native file.
An unchanged, missing or unreadable result still times out instead of reporting
success. Update this helper only; the loaded dev.47 opticapture and dev.44 companion
remain compatible. No device replacement or Live restart is required.

Dev.46 fixes Python shutdown after permission denial and after normal device
completion. Updating this helper from dev.44 takes effect on the next button
click; the dev.44 companion and dev.45 opticapture remain compatible. No restart
or device replacement is required for this helper-only update.

If Accessibility is denied, enable the calling app in System Settings → Privacy
& Security → Accessibility, then retry the button. macOS logs on this Mac identify
Ableton Live 12 Beta for opticapture; the Finder launchers use Terminal. An explicit
action may display the macOS permission prompt; approval remains your decision.

The external launchers below remain available as an alternative.

Development build for macOS / Apple silicon / Live in English. Offline checks
are separate from the pending native save, Browser rename and recall check.

After installing the matching Optimystery device, companion and this folder,
save and restart Live once and use a fresh dev.43 device. The companion must be
active while you record. These commands require Python 3 (already present on
this development Mac) and Accessibility permission for Terminal.

1. Record a loop and stop recording. Keep the source setup loaded.
2. Select the audio clip in Live and double-click **Keep Take.command**. For all
   audio clips in the selected Session scene, use **Keep Scene.command**.
3. To rework it later, select the named audio clip and run **Find Source.command**.
   Finder reveals the saved source Set. Open it when ready, then use **Save Live
   Set As** to make a working copy before editing. The command does not replace
   the Set currently open in Live. Keep refuses to overwrite a retained source Set.

The scripts can also be invoked directly from Terminal. Optional arguments pass
through, for example `Keep Take.command --name "Muffled kit"`. The complete CLI is
`python3 App/optimystery_capture.py keep|scene|find|inspect` from this folder.
For an older recording, `--current-setup` explicitly associates the setup loaded
now. It cannot establish that this setup originally produced that audio.

Keep commands save the active Set, retain a native full Set snapshot (including
MIDI, plug-in state, FX, routing and returns), rename the selected clips and
their WAV/AIFF through Live's Browser, and save again. Avoid editing or changing
Sets while the command runs. Names include clip-derived BPM where unambiguous
and a unique `[opti-…]` identifier. The source Set sits beside the original:
`opti source <id>.als`. `Optimystery Takes/<id>/take.json` records the association,
source tracks, recording observation, portable Opti state, hashes and any parent
take. Audio backups live beside that manifest.

The old audio path is retained as a copy so other Sets and backups remain usable.
Keep these files with the Project. This is a local snapshot, not an archive that
collects every externally referenced sample or installs missing plug-ins.
Copying the audio into another local Project retains source lookup through its
identifier; Find Source still needs the original Project to remain available.

The snapshot captures state when Keep is invoked. It does not replay earlier
manual changes or reproduce random LFO phase. Save the audio as the performance
reference. Portable pattern-editor state and relative LFO target identities are
stored in Optimystery's native state; reopen verification is still pending.

If anything fails, the command reports failure and the snapshot/manifest location.
It never reports success solely because a keystroke was sent. Incomplete takes
retain the pre-rename source Set and original audio paths; clip names or Browser
names may have changed before the failure. Do not repeatedly retry a native UI
failure: retain the message for diagnosis. Find Source can reveal incomplete
captures too. It never attempts an unattended repair or opens the source Set.

## Installation and removal

Copy this folder to `<User Library>/Optimystery Capture/`. Its layout is three
`.command` launchers, this guide, and `App/` containing the Python sources,
`native.swift` and compiled `capture-native`. Make the commands and helper
executable if your extraction tool loses permissions. The development installer
does this and preserves the existing project index and mailbox.

No downloads, third-party devices, sockets, MIDI ports or background desktop
service are used. The existing companion polls a local JSON mailbox, observes
recording starts and accepts explicit selection/name/export commands. The user
command launches its own Accessibility helper for native Save and Browser Rename.
It writes only the requested Project files, capture metadata and local index.
Remove this folder to remove the commands; remove/deselect the companion to stop
its observation/mailbox. Keep saved `Optimystery Takes` and source Sets for recall.

Native mechanisms: [Live Clip file naming](https://www.ableton.com/en/manual/clip-view/#clip-name),
[Live API Clip](https://docs.cycling74.com/apiref/lom/clip/),
[Song selection](https://docs.cycling74.com/apiref/lom/song_view/),
[pattr storage](https://docs.cycling74.com/reference/pattr/).
