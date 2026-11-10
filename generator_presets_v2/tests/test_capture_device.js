"use strict";
const assert = require("assert"), fs = require("fs"), path = require("path"), os = require("os"), vm = require("vm");
const {EventEmitter} = require("events"), {PassThrough} = require("stream");
const device = path.resolve(process.env.CAPTURE_DEVICE_DIR || path.join(__dirname,"../capture/device"));
const {Controller, commandArgs, libraryFrom, readStatus} = require(path.join(device,"optimystery_capture_node.js"));
const temp = fs.mkdtempSync(path.join(os.tmpdir(),"capture-device-test-"));
let checks = 0;
function check(label, fn) { fn(); checks++; console.log("PASS " + label); }
function status(now=1000) { return {schema:1,pid:123,session:"live-one",updated:now,set_path:"/project/A song.als",
    targets:{protocol:1,context:3,clip:{id:"clip-a",name:"Pad $(`never executed`) 日本語",path:"/project/a file.wav",track_name:"Audio"},
        scene:{id:"scene-a",name:"First scene",count:1,clips:[{id:"clip-a",name:"A",path:"/project/a file.wav"}]},busy:false,recording:false}}; }
function processDouble() { const p = new EventEmitter(); p.stdout = new PassThrough(); p.stderr = new PassThrough(); p.stdin = new PassThrough(); return p; }
function uiFixture(onOutput=()=>{}) {
    const patch=JSON.parse(fs.readFileSync(path.join(device,"opticapture.maxpat"),"utf8")).patcher;
    const boxes=Object.fromEntries(patch.boxes.filter(x=>x.box.varname).map(x=>[x.box.varname,structuredClone(x.box)]));
    const calls=[],messages={};
    const context={JSON,Number,Task:function(){this.schedule=()=>{};this.cancel=()=>{};},
        arrayfromargs:args=>Array.from(args),outlet:(...args)=>{calls.push(args);onOutput(args);},
        patcher:{getnamed:name=>boxes[name] ? {
            getattr:key=>boxes[name][key],
            message:(command,...args)=>{messages[name]=[command,...args];boxes[name][command]=args.length===1?args[0]:args;}
        } : null}};
    vm.createContext(context);vm.runInContext(fs.readFileSync(path.join(device,"optimystery_capture_ui.js"),"utf8"),context);
    function click(name) {
        const b=boxes[name];
        if(b.active===0)return;
        // Only the documented mode-0 mouse -> bang behavior is doubled here.
        // Traverse the shipped patch wires/messages; do not invoke the handler directly.
        assert.equal(b.maxclass,"live.text");assert.equal(b.mode,0);
        const edge=patch.lines.map(x=>x.patchline).find(x=>x.source[0]===b.id&&x.source[1]===0);
        const message=Object.values(boxes).find(x=>x.id===edge.destination[0]);
        assert.equal(message.maxclass,"message");
        assert(patch.lines.some(x=>x.patchline.source[0]===message.id&&x.patchline.destination[0]===boxes.ui.id));
        const [method,...args]=message.text.split(" ");context[method](...args);
    }
    return {context,calls,messages,boxes,click};
}
try {
    check("read-only discovery rejects stale and ambiguous companion sessions", () => {
        const root=path.join(temp,"Optimystery Capture/Bridge/123");fs.mkdirSync(root,{recursive:true});
        const s=status();fs.writeFileSync(path.join(root,"status.json"),JSON.stringify(s));
        assert.equal(readStatus(temp,1000000).session,"live-one");
        assert.throws(()=>readStatus(temp,1006000),/Waiting/);
        const other=path.join(temp,"Optimystery Capture/Bridge/456");fs.mkdirSync(other);
        fs.writeFileSync(path.join(other,"status.json"),JSON.stringify({...s,pid:456}));
        assert.throws(()=>readStatus(temp,1000000),/More than one/);
        fs.rmSync(other,{recursive:true});
        delete s.targets;fs.writeFileSync(path.join(root,"status.json"),JSON.stringify(s));
        assert.throws(()=>readStatus(temp,1000000),/dev.44/);
    });
    check("fixed CLI argument array preserves spaces Unicode and shell metacharacters", () => {
        const args=commandArgs(temp,"keep",status(),true);
        assert.equal(args[1],"keep");assert(args.includes("--current-setup"));
        const target=JSON.parse(args[args.indexOf("--target")+1]);
        assert.equal(target.clip_id,"clip-a");assert.equal(target.expected_clips[0].name,status().targets.clip.name);
        assert.equal(args[args.indexOf("--library")+1],temp);
        assert.throws(()=>commandArgs(temp,"anything",status(),false),/Unknown/);
        const s=status();s.targets.scene.count=129;assert.throws(()=>commandArgs(temp,"scene",s,false),/128/);
        s.targets.recording=true;assert.throws(()=>commandArgs(temp,"keep",s,false),/Stop recording/);
        assert(!commandArgs(temp,"find",s,true).includes("--current-setup"));
    });
    check("loading and polling never launch helpers; action freezes the displayed revision", () => {
        let s=status(), launches=[], views=[];
        const child=processDouble();
        const c=new Controller(temp,v=>views.push(v),{now:()=>1000000,readStatus:()=>JSON.parse(JSON.stringify(s)),
            spawn:(file,args,options)=>{launches.push({file,args,options});return child;}});
        c.poll();const shown=c.revision;c.poll();assert.equal(launches.length,0);
        s.targets.clip.id="clip-b";c.poll();
        assert(c.action("keep",shown,1));assert(!c.action("scene",shown,1));
        assert.equal(launches.length,1);assert.equal(launches[0].options.shell,false);
        const target=JSON.parse(launches[0].args[launches[0].args.indexOf("--target")+1]);assert.equal(target.clip_id,"clip-a");
        assert(!views.at(-1).canKeep);assert.equal(views.at(-1).phase,"busy");
        child.stdout.emit("data",'{"type":"progress","message":"Naming');
        child.stdout.emit("data",' audio"}\n');assert.equal(views.at(-1).message,"Naming audio");
        child.stdout.emit("data",'{"type":"complete","count":1,"message":"ok"}\n');
        assert.equal(views.at(-1).phase,"busy"); // completion event alone is not success
        child.emit("close",0,null);assert.equal(views.at(-1).phase,"saved");assert(views.at(-1).hasDetails);
        assert(fs.readFileSync(c.lastFile,"utf8").includes("Naming audio"));
        c.poll();assert.equal(views.at(-1).phase,"ready"); // a new target must not inherit Saved
        assert(views.at(-1).hasDetails);c.stop();
    });
    check("missing completion, failed exit and errors never display Saved", () => {
        for (const scenario of ["missing","nonzero","error"]) {
            const p=processDouble();let view;
            const c=new Controller(temp,v=>view=v,{now:()=>1000000,readStatus:()=>status(),spawn:()=>p});
            c.poll();c.action("keep",c.revision,0);
            if (scenario==="nonzero") {p.stdout.emit("data",'{"type":"complete","count":1}\n');p.stderr.emit("data","Accessibility denied\nSource snapshot retained: /project/source.als");}
            if(scenario==="error")p.emit("error",new Error("spawn ENOENT"));
            p.emit("close",scenario==="nonzero"?1:0,null);assert.equal(view.phase,"failed");assert(view.hasDetails);
            c.stop();
        }
    });
    check("device shutdown closes the ownership pipe and ignores late UI callbacks", () => {
        const p=processDouble();let count=0;
        const c=new Controller(temp,()=>count++,{now:()=>1000000,readStatus:()=>status(),spawn:()=>p});
        c.poll();c.action("keep",c.revision,0);c.stop();const atStop=count;
        assert(p.stdin.writableEnded);p.stdout.emit("data",'{"type":"progress","message":"late"}\n');
        p.emit("close",1,null);assert.equal(count,atStop);assert(!c.action("keep",1,0));
    });
    check("expired displayed targets are rejected before any helper is launched", () => {
        let now=1000000, calls=0, view;
        const c=new Controller(temp,v=>view=v,{now:()=>now,readStatus:()=>status(),spawn:()=>calls++});
        c.poll();now+=6000;assert(!c.action("keep",c.revision,0));assert.equal(calls,0);assert.equal(view.phase,"failed");c.stop();
    });
    check("library path is structural and requires the matching installed helper", () => {
        const folder=path.join(temp,"Max Audio Effect/opticapture"),app=path.join(temp,"Optimystery Capture/App");
        assert.throws(()=>libraryFrom(folder),/helper is missing/);
        fs.mkdirSync(app,{recursive:true});fs.writeFileSync(path.join(app,"optimystery_capture.py"),"test only");
        assert.equal(libraryFrom(folder),temp);assert.throws(()=>libraryFrom(path.join(temp,"Desktop/Capture")),/complete/);
    });
    check("native UI waits for initialized active context and ready; never replays on recall", () => {
        const {context,calls,messages,click}=uiFixture();
        context.init();context.context(0);assert.equal(calls.length,0);context.context(1);
        assert.deepEqual(calls[0],[0,"script","start"]);context.init();assert.equal(calls.length,1);
        const s={revision:7,phase:"ready",message:"Ready",clip:"Pad",scene:"One",identity:"a",canKeep:true,canScene:true,canFind:true};
        context.state(JSON.stringify(s));context.action("keep");assert.equal(calls.length,1);
        context.ready();click("current_setup");click("keep");click("keep");
        assert.deepEqual(calls[1],[0,"action","keep",7,1]);assert.equal(calls.length,2);
        assert.deepEqual(messages.keep,["active",0]);assert.equal(context.currentSetup,0);
        context.init();assert.equal(calls.length,2);
        context.state(JSON.stringify({...s,phase:"failed",message:"Full error\nrecovery path",hasDetails:true}));
        assert(messages.status);context.details();assert.deepEqual(calls[2],[0,"details"]);
        context.freebang();assert.deepEqual(calls[3],[0,"shutdown"]);
    });
    check("current-setup clicks render on/off and survive polling without triggering a capture",()=>{
        const {context:c,boxes:b,calls,click}=uiFixture();
        const state={revision:7,phase:"failed",message:"This recording has no observed source. (command line: --current-setup).",
            clip:"Pad",scene:"One",identity:"a",canKeep:true,canScene:true,canFind:true};
        c.init();c.context(1);c.state(JSON.stringify(state));click("current_setup");assert.equal(c.currentSetup,0);
        c.ready();click("current_setup");assert.equal(c.currentSetup,1);
        assert.equal(b.current_setup.text,"Use current setup: On");
        assert.deepEqual(Array.from(b.current_setup.activebgcolor),Array.from(b.current_setup.bgoncolor));
        assert(!b.status.set.includes("command line"));assert(b.status.set.includes("Current setup selected"));
        for(let i=0;i<5;i++)c.state(JSON.stringify({...state,revision:8+i}));
        assert.equal(c.currentSetup,1);assert.equal(calls.length,1);
        click("current_setup");assert.equal(c.currentSetup,0);assert.equal(b.current_setup.text,"Use current setup: Off");
        assert.deepEqual(Array.from(b.current_setup.activebgcolor),Array.from(b.current_setup.bgcolor));
        click("current_setup");c.state(JSON.stringify({...state,identity:"b"}));assert.equal(c.currentSetup,0);
        click("current_setup");c.context(0);assert.equal(c.currentSetup,0);assert.equal(b.current_setup.active,0);
        c.context(1);c.state(JSON.stringify({...state,phase:"busy"}));click("current_setup");assert.equal(c.currentSetup,0);
        c.state(JSON.stringify({...state,canKeep:false,canScene:false}));click("current_setup");assert.equal(c.currentSetup,0);
    });
    check("built-patch current-setup clicks reach real Node CLI args once for Keep/Scene only",()=>{
        for(const action of ["keep","scene","find"])for(const enabled of [false,true]) {
            const launches=[],p=processDouble();let ui;
            const controller=new Controller(temp,v=>{if(ui)ui.context.state(JSON.stringify(v));},
                {now:()=>1000000,readStatus:()=>status(),spawn:(file,args)=>{launches.push(args);return p;}});
            ui=uiFixture(args=>{if(args[1]==="action")controller.action(...args.slice(2));});
            ui.context.init();ui.context.context(1);ui.context.ready();controller.poll();
            if(enabled)ui.click("current_setup");
            controller.poll();ui.click(action);ui.click(action);
            assert.equal(launches.length,1);
            assert.equal(launches[0].includes("--current-setup"),enabled&&action!=="find");
            assert.equal(ui.context.currentSetup,0);assert.equal(ui.boxes.current_setup.active,0);
            p.emit("close",1,null);assert.equal(ui.boxes.current_setup.active,1);
            assert.equal(ui.boxes.current_setup.text,"Use current setup: Off");controller.stop();
        }
    });
    console.log(checks+" capture-device checks passed");
} finally { fs.rmSync(temp,{recursive:true,force:true}); }
