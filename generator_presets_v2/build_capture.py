#!/usr/bin/env python3
"""Edit a real Max-authored Audio Effect template; never invent an AMXD wrapper.

Offline structural qualification is distinct from a native Max/Live load test.
"""
import argparse
import copy
import json
from pathlib import Path
import shutil
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))
import amxd
from ui_theme import THEME, button_attributes

SOURCES = ("optimystery_capture_node.js", "optimystery_capture_ui.js", "README.md")
GENERATED = ("opticapture.amxd", "opticapture.maxpat", "optimystery_capture_builder.js")
FILES = SOURCES + GENERATED


def resolve_template():
    for app in ("Ableton Live 12 Suite", "Ableton Live 12 Beta"):
        path = Path("/Applications") / (app + ".app/Contents/App-Resources/Misc/Max Devices/Max Audio Effect.amxd")
        if path.is_file():
            if not amxd.round_trips(path):
                raise ValueError("Audio template does not round-trip")
            return path
    raise FileNotFoundError("Install Live's Max Audio Effect template before building Capture")


def build(template=None):
    document = copy.deepcopy(amxd.read_patcher(template or resolve_template()))
    p = document["patcher"]
    # Retain the complete official audio path; remove only instructional comments.
    p["boxes"] = [b for b in p["boxes"] if b["box"].get("text") in ("plugin~", "plugout~")]
    ids = {b["box"]["id"] for b in p["boxes"]}
    p["lines"] = [line for line in p["lines"] if line["patchline"]["source"][0] in ids
                  and line["patchline"]["destination"][0] in ids]
    assert len(p["boxes"]) == 2 and len(p["lines"]) == 2
    p.update(openinpresentation=1, devicewidth=400.0, rect=[0,0,900,650],
             description="opticapture — Keep Take, Keep Scene, Find Source")
    p["project"]["autoorganize"] = 0
    p["dependency_cache"] = [{"name": name, "type":"TEXT", "implicit":1} for name in SOURCES if name.endswith(".js")]

    def add(name, cls, rect, **kw):
        box = dict(id="cap-" + name, varname=name, maxclass=cls, patching_rect=rect,
                   numinlets=1, numoutlets=0, **kw)
        p["boxes"].append({"box":box})
        return box
    def label(name, text, rect, **kw):
        return add(name, "live.comment", rect, text=text, presentation=1, presentation_rect=rect,
                   fontname="Ableton Sans Medium", fontsize=10.0, textcolor=THEME["colors"]["text"], **kw)
    def obj(name, text, x, y, outlets=1):
        b = add(name,"newobj",[x,y,270,22],text=text); b["numoutlets"] = outlets
    def wire(a,b,source=0,destination=0):
        p["lines"].append({"patchline":{"source":["cap-"+a,source],"destination":["cap-"+b,destination]}})
    label("title", "opticapture", [10,7,275,18])
    label("version", (HERE / "VERSION").read_text().strip().split("-")[-1], [338,7,52,18])
    label("clip_label", "Take: select an audio clip", [10,30,380,17])
    label("scene_label", "Scene: select a scene", [10,49,380,17])
    for name, title, x in (("keep","Keep Take",10),("scene","Keep Scene",139),("find","Find Source",268)):
        rect=[x,73,122,27]
        box=add(name,"live.text",rect,text=title,texton=title,mode=0,parameter_enable=0,parameter_mappable=0,
                active=0,presentation=1,presentation_rect=rect,fontname="Ableton Sans Medium",fontsize=11.0,
                annotation={"keep":"Name the displayed audio clip and file together; retain its full source Set.",
                            "scene":"Keep the audio clips in the displayed scene, with their full source Set.",
                            "find":"Reveal the displayed take's linked source Set in Finder."}[name],
                **button_attributes("action"))
        box.update(numoutlets=2,outlettype=["bang", ""])
        b=add(name+"_message","message",[10+(x-10)*2,220,115,22],text="action "+name); b["numoutlets"]=1
        wire(name,name+"_message"); wire(name+"_message","ui")
    rect=[10,106,144,18]
    box=add("current_setup","live.text",rect,text="Use current setup: Off",texton="Use current setup: Off",mode=0,
            active=0,parameter_enable=0,parameter_mappable=0,presentation=1,presentation_rect=rect,
            fontname="Ableton Sans Medium",fontsize=9.0,
            annotation="For older recordings only: explicitly associate the setup loaded now. Enable only when it is the correct source. Resets after each action or target change.",
            **button_attributes("toggle"))
    box.update(numoutlets=2,outlettype=["bang", ""])
    b=add("current_message","message",[10,260,150,22],text="toggle_current"); b["numoutlets"]=1
    wire("current_setup","current_message"); wire("current_message","ui")
    label("setup_hint","for older recordings",[165,106,225,18])
    label("status","Connecting to Live…",[10,130,309,34],linecount=2)
    rect=[330,135,60,20]
    box=add("details","live.text",rect,text="Details",texton="Details",mode=0,parameter_enable=0,
            active=0,presentation=1,presentation_rect=rect,fontname="Ableton Sans Medium",fontsize=9.0,
            annotation="Open the full last result, including source and recovery paths.",**button_attributes("action"))
    box.update(numoutlets=2,outlettype=["bang", ""])
    b=add("details_message","message",[430,260,80,22],text="details"); b["numoutlets"]=1
    wire("details","details_message"); wire("details_message","ui")
    obj("ui","js optimystery_capture_ui.js",330,350)
    obj("node","node.script optimystery_capture_node.js @autostart 0 @watch 0 @restart 0 @defer 1",330,410,2)
    wire("ui","node"); wire("node","ui")
    obj("lifecycle","prepend lifecycle",330,450); wire("node","lifecycle",1); wire("lifecycle","ui")
    obj("thisdevice","live.thisdevice",10,320,3)
    obj("defer_init","deferlow",10,350)
    b=add("init_message","message",[10,380,60,22],text="init"); b["numoutlets"]=1
    wire("thisdevice","defer_init"); wire("defer_init","init_message"); wire("init_message","ui")
    obj("context_message","prepend context",10,420); obj("defer_context","deferlow",10,450)
    wire("thisdevice","context_message",2); wire("context_message","defer_context"); wire("defer_context","ui")
    return document


def builder_source(document):
    # Explicit manual fallback inside a new, real Max Audio Effect. No loadbang.
    return '''/* Max-side fallback. Send build in a new empty Max Audio Effect only.
Keep this script and its sibling JS files together. Native verification pending. */
autowatch = 0;
var spec = ''' + json.dumps(document["patcher"],ensure_ascii=True) + ''';
function build() {
    var p = this.patcher, objects = {}, old = [], o = p.firstobject;
    if (p.getnamed("ui")) { post("Capture already built\\n"); return; }
    while (o) {
        if (o !== this.box && o.maxclass !== "comment" && o.maxclass !== "live.comment" &&
            o.maxclass !== "plugin~" && o.maxclass !== "plugout~") {
            post("Use a new empty Max Audio Effect with only this builder and the template audio path.\\n"); return;
        }
        if (o !== this.box) old.push(o);
        o = o.nextobject;
    }
    for (var n=0;n<old.length;n++) p.remove(old[n]);
    for (var i=0;i<spec.boxes.length;i++) {
        var b=spec.boxes[i].box, r=b.patching_rect;
        var args=[r[0],r[1]].concat(b.maxclass === "newobj" ? b.text.split(" ") : [b.maxclass]);
        var object=p.newdefault.apply(p,args); objects[b.id]=object;
        if (b.varname) object.varname=b.varname;
        for (var key in b) {
            if (["id","varname","maxclass","patching_rect","numinlets","numoutlets","outlettype"].indexOf(key)>=0) continue;
            if (key === "text" && b.maxclass === "newobj") continue;
            object.message.apply(object,[key === "text" && b.maxclass !== "live.text" ? "set" : key].concat(b[key]));
        }
    }
    for (var j=0;j<spec.lines.length;j++) {
        var l=spec.lines[j].patchline; p.connect(objects[l.source[0]],l.source[1],objects[l.destination[0]],l.destination[1]);
    }
    p.message("openinpresentation",1); p.message("devicewidth",400);
    post("opticapture constructed. Save through Max as opticapture.amxd beside the JS files; reopen through Live for initialization.\\n");
}
'''


def write(output, template=None):
    template=template or resolve_template(); output=Path(output); output.mkdir(parents=True,exist_ok=True)
    document=build(template)
    amxd.write_patcher(template, output / GENERATED[0], document, backup=False)
    (output / GENERATED[1]).write_text(json.dumps(document,indent=2)+"\n")
    (output / GENERATED[2]).write_text(builder_source(document))
    for name in SOURCES:
        source=HERE / "capture/device" / name
        if source.resolve() != (output/name).resolve(): shutil.copyfile(source,output/name)
    return output / GENERATED[0]


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=HERE / "capture/device")
    args=parser.parse_args(); print(write(args.output_dir))
