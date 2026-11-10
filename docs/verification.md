# Verification record — 10 November 2026

Initial import is the opticapture subset of Optimystery
`2.0.0-alpha.20-dev.51`, with the dev.50 helper/companion UX changes. Capture
runtime, generated device, tests and companion files were copied byte-for-byte
from the qualified source; the complete main Optimystery project and personal
Sets, recordings, capture history, native logs and local paths were not imported.

The source package supplying this snapshot had SHA256
`5adc49e88eb7590d8e30d598760eaad02f896058a42210456f56b43fdca8b3fa`.
The full source package is not part of this repository.

The dedicated checkout passes **58 Python tests and 10 Node/UI checks**. A build
into a temporary directory reproduces all six device files byte-for-byte. The
Swift source/binary fingerprints match `native-build.json`, and every relative
Python import of the bundled companion resolves within its included modules.
No external sibling checkout is needed for these capture build/test commands.

Capture tests cover real temporary files and Python subprocess shutdown,
transaction recovery, missing/partial/replaced native-save files, source naming,
clip/file ordering, target/session guards, view restoration and timing records.
Node tests traverse the shipped patch's button/message wiring and exercise the
actual UI/controller code. Live and native UI interactions are doubled; their
interfaces are not a native acceptance result.

User-confirmed: Keep Take completed with the expected source-derived clip/file
name; retained source and audio hashes and saved references were checked read-only.

Still requiring user-performed Live checks: prior-view restoration and total
capture duration, Keep Scene, Find Source/manual reopening, source sound/state,
and recall after further project edits. Native fallback builder execution also
remains unverified. No Live action is run by this repository's offline tests.
