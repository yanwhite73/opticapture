/* Native Max display/controller. No Live API, file access, persisted actions or autowatch. */
autowatch = 0;
inlets = 1;
outlets = 1;
var initialized = false, activeContext = false, started = false, online = false;
var currentSetup = 0, lastIdentity = "", lastState = null;
var watchdog = new Task(startFailed, this);
var palette = {ready:[0.12,0.11,0.08,1], waiting:[0.46,0.44,0.42,1], busy:[0.52,0.34,0,1],
    saved:[0.08,0.40,0.19,1], found:[0.08,0.40,0.19,1], failed:[0.70,0.12,0.10,1]};
function send(name) {
    var obj = this.patcher.getnamed(name);
    var args = arrayfromargs(arguments).slice(1), flat = [];
    for (var i=0;i<args.length;i++) flat = flat.concat(args[i]);
    if (obj) obj.message.apply(obj, flat);
}
function init() { initialized = true; maybeStart(); }
function context(value) {
    activeContext = Number(value) === 1;
    if (!activeContext) currentSetup = 0;
    maybeStart(); renderCurrentSetup();
}
function maybeStart() {
    if (!initialized || !activeContext || started) return;
    started = true; currentSetup = 0; renderCurrentSetup();
    outlet(0, "script", "start"); watchdog.schedule(15000);
}
function ready() { online = true; watchdog.cancel(); renderCurrentSetup(); }
function startFailed() { if (!online) fatal("Capture could not start. Open the Max console for the Node for Max error."); }
function fatal() {
    state(JSON.stringify({phase:"failed", message:arrayfromargs(arguments).join(" "),
        clip:"Select an audio clip", scene:"Select a scene", canKeep:false, canScene:false, canFind:false}));
}
function state(text) {
    var s;
    try { s = JSON.parse(text); } catch (err) { return; }
    if (s.identity !== lastIdentity || s.phase === "busy") {
        currentSetup = 0;
    }
    lastIdentity = s.identity; lastState = s;
    send("clip_label", "set", "Take: " + s.clip);
    send("clip_label", "annotation", "Keep Take / Find Source target: " + s.clip);
    send("scene_label", "set", "Scene: " + s.scene);
    send("scene_label", "annotation", "Keep Scene target: " + s.scene);
    send("keep", "active", s.canKeep ? 1 : 0);
    send("scene", "active", s.canScene ? 1 : 0);
    send("find", "active", s.canFind ? 1 : 0);
    send("details", "active", s.hasDetails ? 1 : 0);
    renderCurrentSetup();
    var message = (s.phase === "failed" ? "Failed · " : "") + s.message;
    var visible = message;
    if (s.phase === "failed" && /recording has no observed source/.test(s.message)) {
        visible = currentSetup ? "Current setup selected. Click Keep Take or Keep Scene."
            : "Older recording: turn on Use current setup, then click Keep.";
    }
    send("status", "set", visible.split("\n")[0].slice(0,150));
    send("status", "annotation", message + (s.hasDetails ? " Click Details for the full result and recovery paths." : ""));
    send("status", "textcolor", palette[s.phase] || palette.waiting);
}
function canUseCurrent() {
    return online && activeContext && lastState && lastState.phase !== "busy"
        && (lastState.canKeep || lastState.canScene);
}
function renderCurrentSetup() {
    var obj = this.patcher.getnamed("current_setup");
    if (!obj) return;
    // The patch owns the shared-theme off/on colours; JS owns selected state.
    var colour = obj.getattr(currentSetup ? "bgoncolor" : "bgcolor");
    var label = "Use current setup: " + (currentSetup ? "On" : "Off");
    send("current_setup", "text", label); send("current_setup", "texton", label);
    send("current_setup", "activebgcolor", colour);
    send("current_setup", "activebgoncolor", colour);
    send("current_setup", "active", canUseCurrent() ? 1 : 0);
}
function toggle_current() {
    if (!canUseCurrent()) return;
    currentSetup = currentSetup ? 0 : 1;
    state(JSON.stringify(lastState));
}
function action(command) {
    if (!online || !activeContext || !lastState || lastState.phase === "busy") return;
    var allowed = {keep:"canKeep", scene:"canScene", find:"canFind"};
    if (!allowed[command] || !lastState[allowed[command]]) return;
    var revision = lastState.revision, explicit = currentSetup;
    lastState.phase = "busy"; lastState.canKeep = lastState.canScene = lastState.canFind = false;
    lastState.message = "Starting…"; state(JSON.stringify(lastState));
    outlet(0, "action", command, revision, explicit);
}
function details() { if (online) outlet(0, "details"); }
function lifecycle() {
    var args = arrayfromargs(arguments);
    if (args[0] === "terminated" || ((args[0] === "start" || args[0] === "loadend") && args[1] === "error")) {
        online = false; fatal("Capture process stopped. Check the Max console, then remove and add Capture again.");
    }
}
function freebang() { watchdog.cancel(); if (started) outlet(0, "shutdown"); }
