/* Node for Max entry point. Standard library only; never a shell or network server. */
"use strict";
const fs = require("fs"), path = require("path"), cp = require("child_process");

function libraryFrom(directory) {
    if (path.basename(path.dirname(directory)) !== "Max Audio Effect")
        throw new Error("Keep the complete opticapture folder in User Library / Max Audio Effect.");
    const library = path.dirname(path.dirname(directory));
    if (!fs.existsSync(path.join(library, "Optimystery Capture", "App", "optimystery_capture.py")))
        throw new Error("Capture helper is missing. Install the matching Optimystery Capture package.");
    return library;
}

function readStatus(library, now = Date.now()) {
    const root = path.join(library, "Optimystery Capture", "Bridge");
    const statuses = [];
    for (const entry of fs.existsSync(root) ? fs.readdirSync(root) : []) {
        if (!/^\d+$/.test(entry)) continue;
        try {
            const file = path.join(root, entry, "status.json");
            if (fs.statSync(file).size > 262144) continue;
            const s = JSON.parse(fs.readFileSync(file, "utf8"));
            if (s.schema === 1 && String(s.pid) === entry && now / 1000 - s.updated >= -2 && now / 1000 - s.updated < 5)
                statuses.push(s);
        } catch (_) { /* atomic replacement / stopped or older companion */ }
    }
    if (statuses.length !== 1) throw new Error(statuses.length ? "More than one Live session is active. Use one Live instance for Capture." :
        "Waiting for the capture companion. Restart Live once after the update.");
    if (!statuses[0].targets || statuses[0].targets.protocol !== 1)
        throw new Error("Capture needs the dev.44 companion. Restart Live once after the update.");
    return statuses[0];
}

function commandArgs(library, action, s, currentSetup) {
    if (!["keep", "scene", "find"].includes(action)) throw new Error("Unknown capture action");
    const t = s.targets, scene = action === "scene", chosen = scene ? t.scene : t.clip;
    if (!chosen || (scene && !chosen.count)) throw new Error(scene ? "Select a scene with audio clips" : "Select an audio clip");
    if (scene && chosen.count > 128) throw new Error("Capture is limited to 128 audio clips");
    if (action !== "find" && (t.recording || t.busy)) throw new Error("Stop recording and wait for the roll to finish");
    const target = {target_context: t.context, set_path: s.set_path,
        expected_clips: (scene ? chosen.clips : [chosen]).map(c => ({id:c.id, path:c.path, name:c.name}))};
    target[scene ? "scene_id" : "clip_id"] = chosen.id;
    const args = [path.join(library, "Optimystery Capture", "App", "optimystery_capture.py"), action,
        "--library", library, "--device", "--session", s.session, "--target", JSON.stringify(target)];
    if (currentSetup && action !== "find") args.push("--current-setup");
    return args;
}

class Controller {
    constructor(library, emit, options = {}) {
        this.library = library; this.emit = emit;
        this.spawn = options.spawn || cp.spawn; this.now = options.now || Date.now;
        this.readStatus = options.readStatus || readStatus;
        this.snapshots = new Map(); this.revision = 0; this.child = null;
        this.phase = "waiting"; this.message = "Connecting to Live";
        this.lastFile = null; this.timer = null; this.closed = false; this.last = null;
    }
    view(status) {
        const t = status && status.targets;
        const clip = t && t.clip, scene = t && t.scene;
        return {revision:this.revision, phase:this.phase, message:this.message,
            clip:clip ? clip.name + " — " + clip.track_name : "Select an audio clip",
            scene:scene ? scene.name + " · " + scene.count + " audio clip(s)" : "Select a scene",
            identity:status ? JSON.stringify([status.session, t.context, clip && clip.id, scene && scene.id]) : "",
            canKeep:!!(clip && !this.child && !t.busy && !t.recording && status.set_path),
            canScene:!!(scene && scene.count && scene.count <= 128 && !this.child && !t.busy && !t.recording && status.set_path),
            canFind:!!(clip && !this.child), hasDetails:!!this.lastFile};
    }
    publish(status = this.last) { if (!this.closed) this.emit(this.view(status)); }
    poll() {
        if (this.closed || this.child) return; // Freeze the displayed target throughout the action.
        try {
            const s = this.readStatus(this.library, this.now());
            const identity = value => value ? JSON.stringify([value.session, value.targets.context,
                value.targets.clip && value.targets.clip.id, value.targets.scene && value.targets.scene.id]) : "";
            if (this.last && identity(s) !== identity(this.last)) this.phase = "ready";
            this.last = s;
            this.snapshots.set(++this.revision, s);
            while (this.snapshots.size > 12) this.snapshots.delete(this.snapshots.keys().next().value);
            if (["waiting", "ready"].includes(this.phase)) {
                this.phase = "ready";
                this.message = s.targets.recording ? "Stop recording before keeping a take" :
                    s.targets.busy ? "Waiting for the current roll" : "Ready · keep before changing the source setup";
            }
            this.publish(s);
        } catch (err) {
            this.last = null; this.snapshots.clear();
            if (!["failed", "saved", "found"].includes(this.phase)) { this.phase = "waiting"; this.message = err.message; }
            this.publish(null);
        }
    }
    start() { this.poll(); this.timer = setInterval(() => this.poll(), 500); }
    action(action, revision, currentSetup) {
        if (this.closed || this.child) return false;
        this.lastFile = null;
        try {
            const s = this.snapshots.get(Number(revision));
            if (!s || this.now()/1000 - s.updated > 5) throw new Error("The displayed target expired. Select the clip again.");
            const args = commandArgs(this.library, action, s, !!Number(currentSetup));
            this.phase = "busy"; this.message = action === "find" ? "Finding source…" : "Keeping take…";
            const child = this.spawn("/usr/bin/python3", args, {shell:false, stdio:["pipe", "pipe", "pipe"]});
            this.child = child; this.last = s; this.publish(s);
            let stdout = "", stderr = "", transcript = "", complete = null, settled = false;
            const finish = (code, error) => {
                if (settled) return; settled = true;
                this.child = null;
                const ok = code === 0 && complete && !error;
                this.phase = ok ? (action === "find" ? "found" : "saved") : "failed";
                this.message = ok ? (action === "find" ? complete.message : "Saved · " + complete.count + " audio file(s) linked to source") :
                    (error || stderr.trim() || "Helper ended without a verified result. Check Details.");
                const detail = new Date(this.now()).toISOString() + "\n" + action + "\n" + this.message + "\n\n" + transcript + "\n" + stderr;
                try {
                    const root = path.join(this.library, "Optimystery Capture", "Logs");
                    fs.mkdirSync(root, {recursive:true});
                    this.lastFile = path.join(root, "capture-" + this.now() + "-" + process.pid + ".txt");
                    fs.writeFileSync(this.lastFile, detail, "utf8");
                } catch (e) { this.message += " · Could not write Details: " + e.message; }
                this.publish(s);
            };
            child.stdout.setEncoding("utf8"); child.stderr.setEncoding("utf8");
            child.stdout.on("data", chunk => {
                transcript = (transcript + chunk).slice(-1048576); stdout += chunk;
                let pos;
                while ((pos = stdout.indexOf("\n")) !== -1) {
                    const line = stdout.slice(0,pos); stdout = stdout.slice(pos+1);
                    try {
                        const event = JSON.parse(line);
                        if (event.type === "complete") complete = event;
                        if (event.type === "progress") { this.message = event.message; this.publish(s); }
                    } catch (_) { /* diagnostic output is retained, never treated as success */ }
                }
                if (stdout.length > 1048576) stdout = stdout.slice(-1048576);
            });
            child.stderr.on("data", chunk => { stderr = (stderr + chunk).slice(-1048576); });
            child.on("error", err => finish(1, err.message));
            child.on("close", (code, signal) => finish(code, signal ? "Capture helper stopped (" + signal + "). Check Details." : null));
            return true;
        } catch (err) { this.phase = "failed"; this.message = err.message; this.publish(); return false; }
    }
    details() {
        if (!this.lastFile || this.closed) return;
        // User explicitly clicked Details; open only our own text result, never a Set.
        const child = this.spawn("/usr/bin/open", [this.lastFile], {shell:false, stdio:"ignore"});
        child.on("error", e => { this.message = e.message; this.publish(); });
    }
    stop() {
        this.closed = true; clearInterval(this.timer);
        // Closing the ownership pipe requests Python recovery even if Max then kills Node.
        if (this.child && this.child.stdin) this.child.stdin.end();
    }
}

module.exports = {libraryFrom, readStatus, commandArgs, Controller};
if (process.env.MAX_ENV) {
    const max = require("max-api");
    try {
        const controller = new Controller(libraryFrom(__dirname), state => max.outlet("state", JSON.stringify(state)));
        max.addHandler("action", (action, revision, current) => controller.action(action, revision, current));
        max.addHandler("details", () => controller.details());
        max.addHandler("shutdown", () => controller.stop());
        process.once("SIGTERM", () => { controller.stop(); process.exit(0); });
        process.once("SIGINT", () => { controller.stop(); process.exit(0); });
        process.once("exit", () => controller.stop());
        // This message proves the script and handlers loaded (start success does not).
        max.outlet("ready");
        controller.start();
    } catch (err) { max.outlet("fatal", err.message); }
}
