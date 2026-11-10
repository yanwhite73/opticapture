# opticapture · dev.50

Dev.50 helper/companion add prior-view restoration and timings. Restart once for
the companion; this front end needs no replacement from dev.47.

Dev.49 changes the helper only: routed Rack names, exact Clip View sample context
and clip rename after verified file rename. Existing dev.47 instance needs no
replacement or Live restart; the next click loads the helper.

A separate Max Audio Effect with **Keep Take**, **Keep Scene** and **Find Source**.
The stereo input passes directly to the output; Capture does not process audio.

Find it in **User Library → Max Audio Effect → opticapture**. Drag
**opticapture.amxd** onto an audio track or after an instrument. Select
an audio clip (or scene), then check the Take and Scene readouts in the device.
The last audio target is retained when Device View hides the clip selection.
Selecting a MIDI clip clears the take target. Each action uses its displayed
target, even if selection changes before the command reaches Live.

- **Keep Take:** name the displayed clip and its audio file together; retain the
  complete source Set beside the working Set, plus a capture/recovery record.
- **Keep Scene:** do that for the displayed scene's audio clips.
- **Find Source:** reveal linked source Sets in Finder. Open one yourself, then
  Save As a working copy before editing or recapturing it.
- **Use current setup: Off / On:** click to switch grey Off to amber On for an older recording whose source was not observed.
  Enable only if the correct source is loaded now. Off by default; clears after
  an action or target change. A capture stores current state, not past edits.
- **Details:** open the last full result, error and recovery paths.

Record, stop, then Keep before changing the source setup. Stop all recording
before Keep. Actions disable while running. A failed/partial operation is never
reported as Saved; recovery details are retained. Do not remove Capture while
it is working; removal requests cancellation and recovery.

For a first installation, install the complete folder, matching companion and helper.
Dev.48 changes only the helper save-completion wait; updating that helper takes
effect on the next click with the loaded dev.47 opticapture. No device replacement
or Live restart is needed for this update.

Dev.47 repairs the current-setup control with an explicit click/On/Off state.
Replace the loaded opticapture with this dev.47 instance to load its patch and JS;
no Live restart is needed. It uses the installed dev.46 helper and dev.44 companion.
Earlier companions still require the one-time dev.44 update/restart. Existing dev.43 Optimystery instances already
have capture/portable-state support; the generator's runtime is unchanged apart
from its version. Older generator instances require a fresh dev.44 device.

macOS Apple silicon, English Live UI and Python 3 are currently required for the
native helper. The first action requests macOS Accessibility permission if needed.
Enable the calling application in System Settings → Privacy & Security →
Accessibility, then retry. On this development Mac, macOS identifies Ableton Live
12 Beta for device actions; external Terminal commands need Terminal permission. No Terminal is
opened by this device. The three Finder launchers remain available separately.

This is a development build. Offline tests cover the real code, target guards,
shutdown/recovery, packaging and the unchanged template audio connections.
Native device load/rendering, Node permissions, Browser rename and reopened
source sound/state still require a user-performed Live check. Try a disposable
recording first. No agent has run a capture on your music.

Developer fallback: `opticapture.maxpat` is inspectable source, not an
AMXD renamed from JSON. The shipped AMXD edits Live's real Max-authored Audio
Effect template using the project's round-trip checked container tool. For a
Max-authored rebuild, use a new Max Audio Effect through Live's integrated editor,
add `js optimystery_capture_builder.js` (siblings on the search path), and send
`build` directly to that object. It accepts only the empty template plus itself.
Save through Max beside these files and re-add through Live. The fallback itself
has not been executed in Max. Standalone Max ownership is not required.

Removing this folder removes the front end. Helper, capture index and recorded
take folders remain; installation backups are outside the User Library.
