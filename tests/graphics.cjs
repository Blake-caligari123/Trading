const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict'),cp=require('node:child_process');
const html=fs.readFileSync('index.html','utf8');
for(const [,src] of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))new vm.Script(src);
const ctx={window:{}};vm.runInNewContext(fs.readFileSync('assets/environment.js','utf8'),ctx);
for(const [name,d] of Object.entries(ctx.window.GATEBREAK_MODELS)){
 assert.equal(d.p.length,d.n.length);assert.equal(d.p.length,d.c.length);assert.equal(d.p.length%9,0);
 assert(d.p.every(Number.isFinite));assert(d.c.every(c=>c>=0&&c<=1));
 for(let i=0;i<d.n.length;i+=3)assert(Math.abs(Math.hypot(...d.n.slice(i,i+3))-1)<.001);
 assert(d.p.length/9<6000);console.log(name,d.p.length/9,'triangles');
}
const original=cp.execFileSync('git',['show','c2c02c679e8772c0a5d73c1e62c26eec980771d2:index.html'],{encoding:'utf8'});
const range=(s,a,b)=>{assert(s.includes(a)&&s.includes(b));return s.slice(s.indexOf(a),s.indexOf(b))};
// Runtime deliberately changed by the health pass; behaviour is covered by health.cjs.
assert(html.includes('function runScene(id)'));
assert.equal(range(html,'async function scYard()','/* ============ loop'),range(original,'async function scYard()','/* ============ loop'));
console.log('PASS: syntax, geometry, unchanged story tail');
