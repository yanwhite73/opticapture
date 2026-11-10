#!/usr/bin/env python3
"""Keep Take / Keep Scene / Find Source. Explicit, local, user-run commands."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from capture_core import Capture, find_source, write_json


class Bridge:
    def __init__(self, library, pid):
        self.root = Path(library) / "Optimystery Capture" / "Bridge" / str(pid)
        status = self.root / "status.json"
        if not status.exists():
            raise RuntimeError("The capture companion is not active. Restart Live once after installing the matching companion.")
        self.status = json.loads(status.read_text())
        if time.time() - self.status["updated"] > 10:
            raise RuntimeError("Live's capture companion is not responding")
        self.serial = 0

    def call(self, operation, **data):
        self.serial += 1
        token = "%d-%d-%d" % (os.getpid(), time.time_ns(), self.serial)
        request = dict(data, schema=1, id=token, session=self.status["session"], time=time.time(), operation=operation)
        write_json(self.root / "request.json", request)
        deadline = time.time() + 20
        response_path = self.root / "response.json"
        completed = False
        try:
            while time.time() < deadline:
                if response_path.exists():
                    response = json.loads(response_path.read_text())
                    if response.get("id") == token and response.get("session") == self.status["session"]:
                        completed = True
                        if not response["ok"]:
                            raise RuntimeError(response["error"])
                        return response["result"]
                time.sleep(.08)
            raise RuntimeError("Live did not answer the capture command; no completion was assumed")
        finally:
            if not completed:
                # A paused Live must not later execute a timed-out/cancelled request.
                write_json(self.root / "cancel.json", dict(id=token, session=self.status["session"]))


class Native:
    def __init__(self, executable):
        self.executable = str(executable)
    def invoke(self, *args):
        result = subprocess.run([self.executable] + list(args), capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Native Live operation failed")
        return json.loads(result.stdout)
    def save(self):
        return self.invoke("save")
    def rename(self, old, new, clip):
        return self.invoke("rename", old, new, clip)


@contextmanager
def device_lifetime(enabled=True):
    """Pipe ownership survives even an abrupt Node exit; cancel on stdin EOF.

    A Python exception runs Capture's recovery path. subprocess.run terminates
    and waits for its native child when interrupted, before that recovery.
    """
    if not enabled:
        yield
        return
    stopped = threading.Event()
    previous_handler = signal.getsignal(signal.SIGTERM)
    descriptor = sys.stdin.fileno()
    def cancel(_signal, _frame):
        if stopped.is_set():
            return
        # Only one cancellation: a second signal must not interrupt recovery.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        stopped.set()
        raise RuntimeError("Capture device closed; capture cancelled")
    def watch():
        # Never acquire Python's buffered-stdin lock. Bounded select allows normal
        # success/error exits to stop and join this worker while Node keeps stdin open.
        while not stopped.is_set():
            try:
                readable, _, _ = select.select([descriptor], [], [], .1)
                if not readable:
                    continue
                if os.read(descriptor, 4096):
                    continue
            except OSError:
                pass
            if not stopped.is_set():
                os.kill(os.getpid(), signal.SIGTERM)
            return
    worker = threading.Thread(target=watch, name="opticapture-owner", daemon=False)
    signal.signal(signal.SIGTERM, cancel)
    try:
        worker.start()
        yield
    finally:
        stopped.set()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if worker.ident is not None:
            worker.join()
        signal.signal(signal.SIGTERM, previous_handler)


def target_arguments(raw):
    if raw is None:
        return {}
    target = json.loads(raw)
    allowed = {"target_context", "set_path", "clip_id", "scene_id", "expected_clips"}
    if not isinstance(target, dict) or set(target) - allowed:
        raise ValueError("Invalid capture target")
    if not {"target_context", "set_path", "expected_clips"} <= set(target) or ("clip_id" in target) == ("scene_id" in target):
        raise ValueError("Incomplete capture target")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("keep", "scene", "find", "inspect"))
    parser.add_argument("--name", help="Optional description; tempo and unique take identity are added")
    parser.add_argument("--current-setup", action="store_true", help="Explicitly associate an older/unobserved recording with the setup loaded now")
    parser.add_argument("--library", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--native", type=Path, default=Path(__file__).with_name("capture-native"))
    parser.add_argument("--target", help="Device-displayed target identity (JSON)")
    parser.add_argument("--session", help="Expected companion session")
    parser.add_argument("--device", action="store_true", help="JSON progress and cancellation on device pipe closure")
    args = parser.parse_args(argv)
    target = target_arguments(args.target)
    if args.device:
        if not target or not args.session:
            raise ValueError("Device actions require the displayed target and session")
    with device_lifetime(args.device):
        return run_command(args, target)


def run_command(args, target):
    started = time.monotonic()
    def emit(kind, message, **data):
        if args.device:
            print(json.dumps(dict(type=kind, message=message, elapsed_seconds=round(time.monotonic()-started, 6), **data)), flush=True)
        elif kind != "progress":
            print(message, flush=True)
    emit("progress", "Connecting to Live")
    native = Native(args.native)
    # Permission prompting is only tied to an explicit user action. Read-only
    # inspect (including developer diagnostics) remains non-prompting.
    info = native.invoke("inspect", "--request-access") if args.command != "inspect" else native.invoke("inspect")
    root = args.library / "Optimystery Capture"
    root.mkdir(parents=True, exist_ok=True)
    with (root / "command.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another capture command is running")
        bridge = Bridge(args.library, info["pid"])
        if args.session and bridge.status["session"] != args.session:
            raise ValueError("The displayed Live session changed. Check the target and try again.")
        if args.command == "inspect":
            print(json.dumps(bridge.call("inspect"), indent=2)); return 0
        registry_path = root / "projects.json"
        registry = json.loads(registry_path.read_text()) if registry_path.exists() else []
        if args.command == "find":
            emit("progress", "Finding the retained source Set")
            selection = bridge.call("inspect", **target)
            found = find_source(selection, registry)
            for take in found:
                subprocess.run(["/usr/bin/open", "-R", take["source_set"]], check=True)
            emit("complete", "Revealed %d linked source Set(s)" % len(found), source_sets=[t["source_set"] for t in found])
            return 0
        take = Capture(bridge, native, progress=lambda m: emit("progress", m),
                       timing=lambda report: emit("timing", "Capture stage timings", **report)).run(
            "scene" if args.command == "scene" else "clip", args.name, args.current_setup, target)
        project = str(Path(take["active_set"]).parent / "Optimystery Takes")
        if project not in registry:
            registry.append(project); write_json(registry_path, registry)
        emit("complete", "Kept %d audio file(s). Source: %s" % (len(take["audio"]), take["source_set"]),
             source_set=take["source_set"], take_id=take["id"], count=len(take["audio"]))
        warnings = [warning for item in take["exports"] for warning in item["content"].get("warnings", [])]
        if warnings:
            emit("warning", "Recall needs attention: " + "; ".join(warnings))
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("opticapture: " + str(exc), file=sys.stderr)
        raise SystemExit(1)
