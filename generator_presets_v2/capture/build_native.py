"""Build the first-party macOS helper. Run through the shared workload launcher."""
import hashlib
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
subprocess.run(["/usr/bin/swiftc", "-O", "-target", "arm64-apple-macos13.0", "native.swift", "-o", "capture-native"],
               cwd=HERE, check=True)
(HERE / "native-build.json").write_text(json.dumps(dict(
    target="arm64-apple-macos13.0",
    source_sha256=hashlib.sha256((HERE / "native.swift").read_bytes()).hexdigest(),
    binary_sha256=hashlib.sha256((HERE / "capture-native").read_bytes()).hexdigest()), indent=2) + "\n")
