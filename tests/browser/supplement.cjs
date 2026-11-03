/* Real Edge + isolated backend acceptance. No credential screenshots or storage state. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict'),{execFileSync}=require('child_process');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=process.env.UI_PRIVATE, evidence=process.env.UI_EVIDENCE, origin='http://127.0.0.1:8388';
if(!root||!evidence)throw Error('Explicit isolated private and evidence paths required');
fs.mkdirSync(evidence,{recursive:false});const credentials=JSON.parse(fs.readFileSync(path.join(root,'credentials.json'),'utf8'));
const {A,B,users}=credentials;const results=[],errors=[],contexts=[];let browser;
const save=()=>fs.writeFileSync(path.join(evidence,'results.json'),JSON.stringify({results,unhandled:errors,browser:browser?.version()},null,2));
async function test(id,fn){try{await fn();results.push({id,status:'passed'});save();console.log(id+' passed');}catch(e){results.push({id,status:'failed',message:e.message.replaceAll(credentials.password,'[REDACTED]')});save();throw e;}}
async function wait(fn,timeout=20000){const end=Date.now()+timeout;while(Date.now()<end){if(await fn())return;await new Promise(r=>setTimeout(r,150));}throw Error('Condition timeout');}
async function actor(name){const c=await browser.newContext();contexts.push(c);const p=await c.newPage();p.on('pageerror',e=>errors.push(e.message));p.on('dialog',d=>d.accept());await p.goto(origin);await p.waitForLoadState('networkidle');await p.locator('#username').fill('ui-'+name);await p.locator('#password').fill(credentials.password);await p.locator('#login-submit').click();await p.locator('#app-shell').waitFor({state:'visible'});if(name!=='empty')await wait(()=>p.locator('#current-role').innerText().then(t=>!!t));return {c,p,name};}
async function req(a,method,url,data,extra={}){const me=await a.c.request.get(origin+'/api/auth/me');const token=me.ok()?(await me.json()).csrf_token:'';for(let attempt=0;attempt<5;attempt++){const r=await a.c.request.fetch(origin+url,{method,headers:{Origin:origin,'Content-Type':'application/json','X-CSRF-Token':token,...extra},...(data===undefined?{}:{data})});if(method==='GET'&&r.status()===409&&(await r.json()).detail==='Workspace data changed; retry request'){console.log('Observed documented read-generation conflict; read again');await new Promise(resolve=>setTimeout(resolve,100));continue;}return r;}throw Error('Read remained in conflict');}
const scoped=(w,p)=>`/api/workspaces/${w}${p}`;
function fingerprint(){return JSON.parse(execFileSync((process.env.PYTHON_EXECUTABLE||'python'),['-B','-X','utf8',path.resolve(__dirname,'../../tools/ui_fingerprint.py'),path.join(root,'ui.sqlite3')],{env:{...process.env,TECHDOCS_DATA_ROOT:root},encoding:'utf8'}));}
async function deny(a,method,url,status,data,headers){const before=fingerprint();const r=await req(a,method,url,data,headers);assert.equal(r.status(),status);assert.deepEqual(fingerprint(),before);}
async function select(a,ws){await a.p.locator('#workspace-select').selectOption(ws);await wait(async()=>await a.p.locator('#workspace-select').inputValue()===ws && await a.p.locator('#workspace-body').isVisible());}
async function ready(a,w,t){await wait(async()=>{const r=await req(a,'GET',scoped(w,'/tasks/'+t.id));const s=(await r.json()).state;assert.ok(!['failed','cancelled'].includes(s));return s==='succeeded';},45000);}
async function importDoc(a,w,key,content,label='v1'){const r=await req(a,'POST',scoped(w,'/documents'),{key,title:key,version_label:label,format:'text',content});assert.equal(r.status(),202);const t=(await r.json()).task;await ready(a,w,t);return t;}
async function uiImport(a,key,content,label='v1'){
 await a.p.locator('#import-open').click();const f=a.p.locator('#import-form');await f.locator('[name=key]').fill(key);await f.locator('[name=title]').fill(key);await f.locator('[name=version_label]').fill(label);await f.locator('[name=content]').fill(content);await f.locator('[name=format]').selectOption('text');await a.p.locator('#import-submit').click();await a.p.locator('#import-dialog').waitFor({state:'hidden'});
 await wait(async()=>{const r=await req(a,'GET',scoped(A,'/documents'));const body=await r.json();if(r.status()===409){assert.equal(body.detail,'Workspace data changed; retry request');return false;}assert.equal(r.status(),200,JSON.stringify(body));return body.documents.some(d=>d.key===key&&d.version_label===label&&d.task_state==='succeeded');},45000);
 await wait(()=>a.p.locator('.doc-row').filter({hasText:key}).count().then(n=>n>0));
}

(async()=>{
 await wait(async()=>{try{return (await fetch(origin+'/api/health')).ok;}catch{return false;}},60000);
 browser=await chromium.launch({channel:'msedge',headless:true});let owner=await actor('owner'),editor=await actor('editor'),viewer=await actor('viewer');
 await test('RACE-member-controls',async()=>{
  await owner.p.locator('#members-open').click();let hit=false,release;const gate=new Promise(r=>release=r);
  await owner.p.route(`**/api/workspaces/${A}/members`,async route=>{if(route.request().method()!=='POST')return route.continue();const response=await route.fetch();hit=true;await gate;await route.fulfill({response});});
  await owner.p.locator('#member-username').fill('ui-new');await owner.p.locator('#member-role').selectOption('viewer');await owner.p.locator('#member-submit').click();await wait(()=>hit);
  const row=owner.p.locator('.member-row').filter({hasText:'ui-new'});await row.waitFor();assert.equal(await row.locator('select').isDisabled(),true);assert.equal(await row.getByRole('button',{name:'保存角色'}).isDisabled(),true);
  release();await wait(()=>row.locator('select').isEnabled());assert.equal(await row.getByRole('button',{name:'保存角色'}).isEnabled(),true);assert.equal(await row.getByRole('button',{name:'移除',exact:true}).isEnabled(),true);
  await row.locator('select').selectOption('editor');await owner.p.waitForTimeout(4300);assert.equal(await row.locator('select').inputValue(),'editor');await owner.p.unroute(`**/api/workspaces/${A}/members`);await owner.p.locator('[data-close=members-dialog]').click();
 });
 await test('RACE-stale-errors',async()=>{
  // Exercise the actual browser api wrapper with controlled obsolete transport failures.
  for(const kind of ['network','timeout']){
   const result=await owner.p.evaluate(async kind=>{
    const original=window.fetch;let rejectOld;
    window.fetch=(url,opts)=>String(url).includes('obsolete-contract')?new Promise((resolve,reject)=>{rejectOld=reject;opts.signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')));}):original(url,opts);
    try{
     const old=api('/obsolete-contract',{},context(),'search').then(()=>({unexpected:true}),e=>({name:e.name,online:state.online}));
     const newer=await api(`/api/workspaces/${state.ws}/summary`,{},context(),'search');
     if(kind==='network')rejectOld(new TypeError('Synthetic obsolete network failure'));
     return {newer:typeof newer==='object',old:await old};
    }finally{window.fetch=original;}
   },kind);
   assert.equal(result.newer,true);assert.deepEqual(result.old,{name:'AbortError',online:true});
  }
  const active=await owner.p.evaluate(async()=>{const original=window.fetch;window.fetch=()=>Promise.reject(new TypeError('Current transport failure'));try{await api('/current-contract',{},context(),'search');}catch(e){return {online:state.online,message:e.message};}finally{window.fetch=original;}});
  assert.equal(active.online,false);assert.ok(active.message.includes('网络连接中断'));assert.equal(await owner.p.locator('#import-open').isDisabled(),true);await wait(()=>owner.p.locator('#workspace-body').isVisible(),6000);
 });
 await test('WRITE03',async()=>{
  let calls=0,release;const hold=new Promise(r=>release=r);await owner.p.route(`**/api/workspaces/${A}/documents`,async route=>{if(route.request().method()!=='POST')return route.continue();calls++;const response=await route.fetch();await hold;await route.fulfill({response});});
  await owner.p.locator('#import-open').click();const f=owner.p.locator('#import-form');await f.locator('[name=key]').fill('single-submit');await f.locator('[name=title]').fill('single-submit');await f.locator('[name=content]').fill('Single submission retention document.');
  await f.evaluate(form=>{form.requestSubmit();form.requestSubmit();});await wait(()=>calls===1);assert.equal(await owner.p.locator('#workspace-select').isDisabled(),true);release();await owner.p.locator('#import-dialog').waitFor({state:'hidden'});await owner.p.unroute(`**/api/workspaces/${A}/documents`);
  await wait(async()=>{const r=await req(owner,'GET',scoped(A,'/tasks'));return (await r.json()).tasks.some(t=>t.state==='succeeded');});assert.equal(calls,1);const tasks=await req(owner,'GET',scoped(A,'/tasks'));assert.equal((await tasks.json()).tasks.length,1);
 });
 await test('WRITE03-timeout',async()=>{
  let calls=0,release;const hold=new Promise(r=>release=r);await owner.p.route(`**/api/workspaces/${A}/documents`,async route=>{if(route.request().method()!=='POST')return route.continue();calls++;const response=await route.fetch();await hold;await route.fulfill({response}).catch(()=>{});});
  await owner.p.locator('#import-open').click();const f=owner.p.locator('#import-form');await f.locator('[name=key]').fill('timeout-submit');await f.locator('[name=title]').fill('timeout-submit');await f.locator('[name=content]').fill('Timeout acceptance content.');await owner.p.locator('#import-submit').click();await wait(()=>calls===1);await wait(()=>owner.p.locator('#import-error').innerText().then(t=>t.includes('请求超时')),35000);assert.equal(calls,1);
  const tasks=await req(owner,'GET',scoped(A,'/tasks'));assert.equal((await tasks.json()).tasks.filter(t=>t.title==='timeout-submit').length,1);release();await owner.p.unroute(`**/api/workspaces/${A}/documents`);await owner.p.locator('[data-close=import-dialog]').first().click();
 });
 await test('UI01-nonjson',async()=>{
  await owner.p.route(`**/api/workspaces/${A}/search`,route=>route.fulfill({status:502,contentType:'text/html',body:'upstream failure'}));await owner.p.locator('#query').fill('error');await owner.p.locator('#search-submit').click();await wait(()=>owner.p.locator('#page-error').innerText().then(t=>t.includes('502')));assert.equal(errors.length,0);await owner.p.unroute(`**/api/workspaces/${A}/search`);
 });
 await test('WS02-same-workspace',async()=>{
  let hits=0,firstRelease,secondRelease;const first=new Promise(r=>firstRelease=r),second=new Promise(r=>secondRelease=r);
  await owner.p.route(`**/api/workspaces/${A}/search`,async route=>{const n=++hits;const response=await route.fetch();await (n===1?first:second);await route.fulfill({response}).catch(()=>{});});
  await owner.p.locator('#query').fill('retention');await owner.p.locator('#search-form').evaluate(f=>f.requestSubmit());await wait(()=>hits===1);await owner.p.locator('#query').fill('submission');await owner.p.locator('#search-form').evaluate(f=>f.requestSubmit());await wait(()=>hits===2);firstRelease();await owner.p.waitForTimeout(300);assert.equal(await owner.p.locator('#search-submit').isDisabled(),true);secondRelease();await wait(()=>owner.p.locator('#search-submit').isEnabled());await owner.p.unroute(`**/api/workspaces/${A}/search`);
 });
 await test('TASK01',async()=>{
  const docs=await req(owner,'GET',scoped(A,'/documents'));const doc=(await docs.json()).documents.find(d=>d.key==='single-submit');const fail=JSON.parse(execFileSync((process.env.PYTHON_EXECUTABLE||'python'),['-B',path.resolve(__dirname,'../../tools/ui_failure_fixture.py'),path.join(root,'ui.sqlite3'),doc.id],{env:{...process.env,PYTHONPATH:path.resolve(__dirname,'../..'),TECHDOCS_DATA_ROOT:root},encoding:'utf8'}));
  const search=await req(viewer,'POST',scoped(A,'/search'),{query:'retention',method:'bm25'});assert.equal((await search.json()).results[0].version_id,fail.old_active_version);
  await deny(viewer,'POST',scoped(A,`/tasks/${fail.id}/retry`),403,{});
  await editor.p.locator('#tasks-open').click();const row=editor.p.locator(`[data-task-id="${fail.id}"]`);await row.getByRole('button',{name:'重试'}).waitFor();const [retryResponse]=await Promise.all([editor.p.waitForResponse(r=>r.url().endsWith('/tasks/'+fail.id+'/retry')&&r.request().method()==='POST'),row.getByRole('button',{name:'重试'}).click()]);assert.equal(retryResponse.status(),200);await ready(editor,A,{id:fail.id});await wait(()=>row.innerText().then(t=>t.includes('已完成')));await editor.p.locator('[data-close=tasks-dialog]').click();
 });
 await test('SEC03',async()=>{
  const payload={key:'safe-render',title:'<img src=x onerror="window.injected=1">',version_label:'v1',format:'text',source_url:'https://example.org/reference',content:'<svg onload="window.injected=1"> Render this as text. Safe content evidence.'};const r=await req(owner,'POST',scoped(A,'/documents'),payload);assert.equal(r.status(),202);await ready(owner,A,(await r.json()).task);
  await wait(()=>owner.p.locator('.doc-row').filter({hasText:payload.title}).count().then(n=>n>0));assert.equal(await owner.p.locator('#documents img').count(),0);assert.equal(await owner.p.evaluate(()=>window.injected),undefined);
  await owner.p.locator('#query').fill('Safe content evidence');await owner.p.locator('#method').selectOption('bm25');await owner.p.locator('#search-submit').click();await owner.p.locator('.reader-text').first().waitFor();assert.equal(await owner.p.locator('#reader svg').count(),0);assert.equal(await owner.p.locator('.reader-source').getAttribute('rel'),'noopener noreferrer');assert.equal(await owner.p.evaluate(()=>localStorage.length+sessionStorage.length),0);
  await owner.p.route('**/passages/*',async route=>{const response=await route.fetch();const body=await response.json();body.source_url='javascript:window.injected=1';await route.fulfill({response,json:body});});await owner.p.locator('.result').first().click();await wait(()=>owner.p.locator('.reader-source').count().then(n=>n===0));await owner.p.unroute('**/passages/*');
 });
 await test('AUTH04',async()=>{
  let hit=false,release;const hold=new Promise(r=>release=r);await owner.p.route(`**/api/workspaces/${A}/search`,async route=>{hit=true;await hold;await route.fulfill({status:401,contentType:'application/json',body:'{"detail":"expired old request"}'}).catch(()=>{});});
  await owner.p.locator('#query').fill('delayed');await owner.p.locator('#search-submit').click();await wait(()=>hit);await owner.p.locator('#logout').click();await owner.p.locator('#login-view').waitFor();await owner.p.locator('#password').fill(credentials.password);await owner.p.locator('#login-submit').click();await owner.p.locator('#workspace-body').waitFor();release();await owner.p.waitForTimeout(300);assert.equal(await owner.p.locator('#current-user').innerText(),'ui-owner');assert.equal(await owner.p.locator('#login-view').isVisible(),false);await owner.p.unroute(`**/api/workspaces/${A}/search`);
  await owner.p.route(`**/api/workspaces/${A}/documents`,route=>route.abort('failed'));await wait(()=>owner.p.locator('#page-error').innerText().then(t=>t.includes('网络连接中断')),6000);assert.equal(await owner.p.locator('#import-open').isDisabled(),true);assert.equal(await owner.p.locator('#login-view').isVisible(),false);await owner.p.unroute(`**/api/workspaces/${A}/documents`);await wait(()=>owner.p.locator('#workspace-body').isVisible(),6000);
 });
 await test('UI02',async()=>{
  let active=0,peak=0,count=0;await owner.p.route(`**/api/workspaces/${A}/summary`,async route=>{active++;peak=Math.max(peak,active);count++;const response=await route.fetch();await new Promise(r=>setTimeout(r,500));await route.fulfill({response}).catch(()=>{});active--;});
  await owner.p.waitForTimeout(5000);assert.ok(peak<=1);await owner.p.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));});await owner.p.waitForTimeout(700);const stopped=count;await owner.p.waitForTimeout(4200);assert.equal(count,stopped);
  await owner.p.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));});await owner.p.locator('#workspace-body').waitFor();await owner.p.waitForFunction(()=>!document.body.classList.contains('verifying'));await owner.p.unroute(`**/api/workspaces/${A}/summary`);
 });
 await test('UI03',async()=>{
  await owner.p.setViewportSize({width:390,height:844});await owner.p.locator('#import-open').focus();await owner.p.keyboard.press('Enter');await owner.p.locator('#import-dialog').waitFor();await owner.p.keyboard.press('Escape');await owner.p.locator('#import-dialog').waitFor({state:'hidden'});assert.equal(await owner.p.locator('#import-open').evaluate(el=>el===document.activeElement),true);
  assert.ok(await owner.p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+2));await owner.p.screenshot({path:path.join(evidence,'mobile.png'),fullPage:true});await owner.p.setViewportSize({width:1440,height:1000});await owner.p.screenshot({path:path.join(evidence,'workbench.png'),fullPage:true});
 });
 await test('UI01-extra',async()=>{const d=await req(owner,'GET',scoped(A,'/documents'));const target=(await d.json()).documents.find(x=>x.key==='safe-render');await owner.p.locator('.doc-row').filter({hasText:'<img src=x'}).click();await owner.p.locator('#versions-open').click();await owner.p.locator('#versions-dialog').waitFor();await owner.p.locator('#delete-document').click();await owner.p.locator('#versions-dialog').waitFor({state:'hidden'});await deny(owner,'GET',scoped(A,`/documents/${target.id}/versions`),404);assert.equal(errors.length,0);});
 await test('END-browser',async()=>{for(const a of [owner,editor,viewer]){await a.p.locator('#logout').click();await a.p.locator('#login-view').waitFor();}assert.equal(errors.length,0);});
})().catch(e=>{console.error(e.message.replaceAll(credentials.password,'[REDACTED]'));process.exitCode=1;}).finally(async()=>{for(const c of contexts)await c.close().catch(()=>{});if(browser)await browser.close();save();});

