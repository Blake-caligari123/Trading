// Logic/scene tests with real Three.js geometry; DOM/canvas drawing and GPU rendering are stubbed.
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const THREE=require(process.env.THREE_TEST_PATH||'../assets/vendor/three-r128.min.js');
const no=()=>{};
const draw=new Proxy({getImageData:(x,y,w,h)=>({data:new Uint8ClampedArray(w*h*4),width:w,height:h}),measureText:t=>({width:String(t).length*5}),createRadialGradient:()=>({addColorStop:no}),createLinearGradient:()=>({addColorStop:no})},{get:(o,k)=>k in o?o[k]:no,set:(o,k,v)=>(o[k]=v,true)});
function element(){return {style:{},dataset:{},classList:{add:no,remove:no,toggle:no},children:[],hidden:false,textContent:'',innerHTML:'',getContext:()=>draw,addEventListener(type,fn){(this.listeners[type]??=[]).push(fn)},listeners:{},append(e){this.children.push(e);e.parent=this},get firstChild(){return this.children[0]},remove(){if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this)},focus:no,setPointerCapture:no,querySelector:()=>element(),getBoundingClientRect:()=>({left:0,top:0,width:800,height:600})}}
const els={},events={},doc=element();doc.body=element();doc.getElementById=id=>els[id]??=element();doc.createElement=()=>element();doc.hidden=false;
const storage=new Map([['gatebreak.choices','null']]);let time=0;
THREE.WebGLRenderer=class{constructor(){if(process.env.TEST_NO_WEBGL)throw Error("GPU unavailable");this.shadowMap={};this.domElement=element()}setPixelRatio(){}setSize(){}render(){}};
const c={console,THREE,document:doc,innerWidth:1280,innerHeight:720,devicePixelRatio:1,matchMedia:()=>({matches:false}),performance:{now:()=>time},requestAnimationFrame:no,setTimeout:no,clearTimeout:no,addEventListener:(k,f)=>(events[k]??=[]).push(f),localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},location:{reload:no}};c.window=c;
vm.createContext(c);vm.runInContext(fs.readFileSync('assets/environment.js','utf8'),c);
let src=[...fs.readFileSync('index.html','utf8').matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]).find(s=>s.includes('function newPlayer'));
src=src.replace(/\}\)\(\);\s*$/,`globalThis.test={S,get P(){return P},get M(){return M},get DL(){return DL},get world(){return world},get texCount(){return TEX.size},get choices(){return CHOICES},loadMap,runScene,frame,pause,resume,begin,setMode,resetInput,keys,joy,advance,endDialogue,saveChoice,moveEnt,blocked,pressAttack,releaseAttack,guardDown,guardUp,dash,clearLine,nearestEnemy,spawnEnemy,spawnBoss,swing,useSkill,update,updateEnemy,updateBoss,hurt,doAttack,updateHud,MAPDEF,ORDER,until,wait};})();`);
src=src.replace('render();updateHud();','updateHud();');
vm.runInContext(src,c);const t=c.test;
if(process.env.TEST_NO_WEBGL){assert(els.newBtn.disabled&&els.contBtn.disabled);assert.equal(els.graphicsHelp.hidden,false);t.begin('yard');assert.equal(t.S.mode,'title');console.log('PASS WebGL unavailable: clear message and safe startup');process.exit(0)}
const tick=async(n=1)=>{for(let i=0;i<n;i++){time+=50;t.frame(time);await Promise.resolve()}};
(async()=>{
 assert.deepEqual(Object.keys(t.choices),[]);t.saveChoice('mercy','spare');assert.equal(t.choices.mercy,'spare');
 for(const id of Object.keys(t.MAPDEF)){t.loadMap(id);assert.equal(t.M.solid.length,t.M.w*t.M.h);t.world.traverse(o=>{const a=o.geometry?.attributes?.position?.array;if(a)assert(a.every(Number.isFinite),id+' geometry')});}
 console.log('PASS all map geometry/collision arrays build');
 t.loadMap('estate');const texBefore=t.texCount;for(let i=0;i<8;i++)t.loadMap('estate');assert.equal(t.texCount,texBefore,'map textures leak');
 console.log('PASS repeated map loads keep texture cache bounded');
 t.S.scene='yard';t.setMode('play');t.P.holding=t.P.guarding=true;t.keys.add('KeyW');t.joy.active=true;t.joy.dx=1;t.pause();assert(!t.P.holding&&!t.P.guarding&&!t.keys.size&&!t.joy.active);
 const before=t.S.time;let fired=false;t.wait(.1).then(()=>fired=true);await tick(10);assert.equal(t.S.time,before);assert(!fired);
 events.keydown.forEach(f=>f({code:'Escape',repeat:false,preventDefault:no}));assert.equal(t.S.mode,'play');await tick(4);assert(fired);
 doc.hidden=true;const hiddenTime=t.S.time;await tick(5);assert.equal(t.S.time,hiddenTime);doc.hidden=false;
 console.log('PASS pause/hidden clock, waiters, Escape resume and input reset');
 t.P.atkLock=9;t.P.dashT=9;t.P.stT=9;t.begin('yard');assert.equal(t.P.atkLock,0);assert.equal(t.P.dashT,0);assert.equal(t.P.stT,0);assert.equal(Object.keys(t.choices).length,0);assert.equal(storage.get('gatebreak.choices'),'{}');
 for(const id of t.ORDER){t.runScene(id);await tick(160);assert(['cut','dialogue','play','end'].includes(t.S.mode),id);assert(Number.isFinite(t.P.x)&&Number.isFinite(t.P.y),id);console.log('PASS scene entry',id,t.S.mode)}
 t.runScene('yard');await tick(100);for(let i=0;i<500&&t.S.mode!=='play';i++){if(t.DL)t.advance();await tick(1)}assert.equal(t.S.mode,'play');
 const hp=t.P.hp,st=t.P.st;t.pressAttack();assert(t.P.st<st);t.releaseAttack();t.guardDown();assert(t.P.guarding);t.guardUp();t.dash();assert(t.P.dashing>0);await tick(10);assert(Number.isFinite(t.P.hp)&&t.P.hp<=hp);
 console.log('PASS opening dialogue to spar, attack stamina, guard and dash');
 const x=t.P.x,y=t.P.y;t.moveEnt(t.P,-10000,0,4);assert(t.P.x>=0&&t.P.x<t.M.w*16);assert(!t.blocked(t.P.x,t.P.y,4));
 console.log('PASS movement cannot cross map boundary');
 // Deterministic arenas isolate collision, defence and recovery regressions.
 const arena=()=>{t.loadMap('estate');t.M.solid.fill(0);t.M.actors=[];t.M.allies=[];t.M.enemies=[];t.P.x=80;t.P.y=80;t.P.hp=100;t.P.maxHp=100;t.P.st=100;t.P.stMax=100;t.P.inv=0;t.P.dashing=0;t.P.stun=0;t.P.guarding=false;t.P.holding=false;t.P.cds={};t.P.morrow=true;t.P.corr=60;t.P.atkLock=0;t.P.counter=0;t.S.sparOn=false;t.S.noDeath=false;t.S.freeze=0;t.setMode('play')};
 arena();const foe=t.spawnEnemy('bandit',112,80,{state:'recover',cd:10});for(let y=0;y<t.M.h;y++)t.M.solid[y*t.M.w+6]=1;
 assert(!t.clearLine(80,80,112,80));assert.equal(t.nearestEnemy(100),null);const foeHP=foe.hp;t.swing(0,60,3,1,false);assert.equal(foe.hp,foeHP);t.useSkill('wave');assert.equal(foe.hp,foeHP);
 t.M.solid.fill(0);assert(t.clearLine(80,80,112,80));assert.equal(t.nearestEnemy(100),foe);t.swing(0,60,3,1,false);assert(foe.hp<foeHP);
 console.log('PASS walls block lock-on, melee and wave; open attacks connect');
 arena();t.P.stun=1;t.useSkill('rend');assert.equal(t.P.dashing,0);assert.equal(t.P.cds.rend,undefined);t.P.stun=0;t.P.guarding=true;t.P.holding=true;t.P.charge=.8;t.useSkill('wave');assert(!t.P.guarding&&!t.P.holding);assert.equal(t.P.charge,0);
 console.log('PASS stunned skills rejected; casting clears guard and charge');
 arena();t.P.dir='right';t.guardDown();const striker=t.spawnEnemy('bandit',89,80,{state:'strike',stT:.01,lx:-1,ly:0,aggro:true});t.updateEnemy(striker,.02);assert.equal(striker.state,'broken');assert.equal(t.P.hp,100);assert(t.P.counter>0);
 arena();t.P.dir='right';t.guardDown();const boss=t.spawnBoss(92,80);Object.assign(boss,{state:'strike',stT:.01,lx:-1,ly:0,hitOnce:false,combo:3});t.updateEnemy(boss,.02);assert.equal(boss.state,'broken');assert.equal(t.P.hp,100);
 console.log('PASS last-frame melee and boss parries retain the punish window');
 arena();t.P.hp=90;t.P.combatT=10;t.M.drops.push({x:80,y:80,vx:0,vy:0,t:1});t.update(.01);assert.equal(t.P.hp,100);assert.equal(t.M.drops.length,0);assert(t.M.nums.some(n=>n.v==='+10'));
 t.M.drops.push({x:80,y:80,vx:0,vy:0,t:1});t.update(.01);assert.equal(t.M.drops.length,1);assert(Number.isFinite(t.M.drops[0].x));
 console.log('PASS exact-position pickups stay finite, report actual healing, remain at full HP');
 arena();t.P.hp=1;t.M.drops.push({x:80,y:80,vx:0,vy:0,t:1});t.M.projs.push({x:80,y:76,vx:0,vy:0,t:1,r:2,dmg:100});t.update(.01);assert.equal(t.S.mode,'dead');assert.equal(t.P.hp,0);assert.equal(t.M.drops.length,1);
 console.log('PASS lethal projectile stops pickup healing in the same frame');
 arena();t.P.corr=30;t.runScene('temple');t.P.corr=88;t.saveChoice('mercy','strike');t.runScene('temple');assert.equal(t.P.corr,30);assert.equal(t.choices.mercy,undefined);
 console.log('PASS retries restore chapter-entry corruption and choices');
 arena();t.P.hp=80;t.P.combatT=0;const threat=t.spawnEnemy('bandit',130,80,{aggro:true,state:'recover',stT:10});t.update(.05);assert.equal(t.P.hp,80);threat.dead=true;t.P.combatT=0;t.update(.05);assert(t.P.hp>80);
 console.log('PASS nearby combat blocks regeneration; recovery resumes after combat');
 arena();t.S.talk=null;t.P.holding=true;t.P.charge=.5;t.P.dashT=.55;t.updateHud();assert.equal(els.atkLbl.textContent,'RELEASE');assert.equal(els.dashCd.style.height,'100%');t.P.dashT=0;t.dash();assert(!t.P.holding);assert.equal(t.P.charge,0);t.pressAttack();assert(!t.P.holding);t.P.buf=.2;t.pause();assert.equal(t.P.buf,0);
 console.log('PASS charge/cooldown feedback and dash/pause cancel pending attacks');
 arena();t.S.flags.stairs=true;t.S.slow=1;t.S.freeze=1;t.P.path={pts:[[1,1]],spd:1};t.runScene('whisper');assert.equal(t.S.flags.stairs,undefined);assert.equal(t.S.slow,0);assert.equal(t.S.freeze,0);assert.equal(t.P.path,null);
 console.log('PASS scene retry clears exploration flags and transient animation state');


 // Scripted progression exercises story waits and choices, not combat difficulty or rendering.
 for(const mercy of ['spare','strike'])for(const brother of ['help','leave']){
  t.begin('yard');const visited=new Set();let stage=0;
  for(let i=0;i<16000&&t.S.mode!=='end';i++){
   if(!visited.has(t.S.scene))console.log('  story enters',t.S.scene);visited.add(t.S.scene);t.P.hp=t.P.maxHp;
   if(t.DL){if(t.DL.choices&&t.DL.i===t.DL.lines.length-1){t.endDialogue(t.DL.choices.some(a=>a[1]===mercy)?mercy:t.DL.choices.some(a=>a[1]===brother)?brother:'take')}else t.advance()}
   if(t.S.mode==='play'){
    switch(t.S.scene){
     case 'yard':t.S.spar='kael';t.S.lilaT=0;break;
     case 'night':t.S.fightT=12;break;
     case 'whisper':{const door=t.M.objects.find(o=>o.id==='fatherdoor'),book=t.M.objects.find(o=>o.id==='bookcase');if(!door.open&&door.act)door.act();else if(!book.slid&&book.act&&book.cond())book.act();else if(t.S.flags.stairs){t.P.x=200;t.P.y=24}break}
     case 'chamber':t.P.y=190;break;
     case 'forest':t.P.x=632;t.P.y=200;break;
     case 'temple':{for(const e of t.M.enemies)e.dead=true;if(t.S.obj==='Defeat Hesk')t.S.bossDown=true;else{const r=t.M.rooms[t.choices.brother?4:t.choices.mercy?3:2];t.P.x=r.cx;t.P.y=r.cy}break}
    }
   }
   await tick(1);
  }
  assert.equal(t.S.mode,'end',mercy+'/'+brother+' stopped in '+t.S.scene+' '+t.S.obj);assert.equal(visited.size,9);assert.equal(t.choices.mercy,mercy);assert.equal(t.choices.brother,brother);assert.equal(storage.get('gatebreak.prologue.v1'),'end');
  console.log('PASS complete scripted story',mercy,brother);
 }

 // Reboot the actual game script against saved storage to test Continue, not just in-memory retry.
 storage.set('gatebreak.prologue.v1','temple');storage.set('gatebreak.checkpoint.v1',JSON.stringify({scene:'temple',corr:35,choices:{}}));storage.set('gatebreak.choices',JSON.stringify({mercy:'strike'}));
 vm.runInContext(src,c);c.test.begin('temple');assert.equal(c.test.P.corr,35);assert.equal(c.test.choices.mercy,undefined);
 storage.set('gatebreak.checkpoint.v1',JSON.stringify({scene:'temple',corr:-1,choices:null}));vm.runInContext(src,c);c.test.begin('temple');assert.equal(c.test.P.corr,10);
 console.log('PASS Continue reloads entry checkpoint; invalid checkpoint safely falls back');
})().catch(e=>{console.error(e);process.exitCode=1});
