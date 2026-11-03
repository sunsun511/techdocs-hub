/* Targeted checks for the 2026-11-03 UI changes: tab-return state and permission re-check,
   corrected translation, highlighting, welcome area. Run like e2e.cjs on a NEW synthetic database. */
const fs=require('fs'),path=require('path'),assert=require('assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=process.env.UI_PRIVATE, evidence=process.env.UI_EVIDENCE, origin='http://127.0.0.1:8388';
if(!root||!evidence)throw Error('Explicit isolated private and evidence paths required');
fs.mkdirSync(evidence,{recursive:false});const credentials=JSON.parse(fs.readFileSync(path.join(root,'credentials.json'),'utf8'));
const {A,B,users}=credentials;const results=[],errors=[],contexts=[];let browser;
const timeline=[];const mark=(event,detail={})=>timeline.push({t:Date.now(),event,...detail});
const save=()=>fs.writeFileSync(path.join(evidence,'results.json'),JSON.stringify({results,unhandled:errors,browser:browser?.version(),timeline},null,2));
async function test(id,fn){try{await fn();results.push({id,status:'passed'});save();console.log(id+' passed');}catch(e){results.push({id,status:'failed',message:e.message.replaceAll(credentials.password,'[REDACTED]')});save();throw e;}}
async function wait(fn,timeout=20000){const end=Date.now()+timeout;while(Date.now()<end){if(await fn())return;await new Promise(r=>setTimeout(r,150));}throw Error('Condition timeout');}
async function actor(name){const c=await browser.newContext();contexts.push(c);const p=await c.newPage();p.on('pageerror',e=>errors.push(e.message));p.on('dialog',d=>d.accept());await p.goto(origin);await p.waitForLoadState('networkidle');await p.locator('#username').fill('ui-'+name);await p.locator('#password').fill(credentials.password);await p.locator('#login-submit').click();await p.locator('#app-shell').waitFor({state:'visible'});await wait(()=>p.locator('#current-role').innerText().then(t=>!!t));return {c,p,name};}
async function req(a,method,url,data){const me=await a.c.request.get(origin+'/api/auth/me');const token=me.ok()?(await me.json()).csrf_token:'';return a.c.request.fetch(origin+url,{method,headers:{Origin:origin,'Content-Type':'application/json','X-CSRF-Token':token},...(data===undefined?{}:{data})});}
const scoped=(w,p)=>`/api/workspaces/${w}${p}`;
async function importDoc(a,w,key,content){const r=await req(a,'POST',scoped(w,'/documents'),{key,title:key,version_label:'v1',format:'text',content});assert.equal(r.status(),202);const t=(await r.json()).task;
  await wait(async()=>{const s=(await (await req(a,'GET',scoped(w,'/tasks/'+t.id))).json()).state;assert.ok(!['failed','cancelled'].includes(s));return s==='succeeded';},45000);}
async function setHidden(p,hidden){await p.evaluate(h=>{Object.defineProperty(document,'hidden',{configurable:true,value:h});document.dispatchEvent(new Event('visibilitychange'));},hidden);}
// Waits for THIS search: the matching response, then freshly rendered results and reader (old nodes are tagged
// before the click, so leftover results from an earlier search can never satisfy the wait).
const searchBody=r=>JSON.parse(r.request().postData()||'{}');
// Match one specific search by its request body, never 'whichever /search response comes next'.
const isSearch=(r,q,extra=b=>true)=>r.url().endsWith('/search')&&r.request().method()==='POST'&&searchBody(r).query===q&&extra(searchBody(r));
async function tagStale(p){await p.evaluate(()=>{for(const el of document.querySelectorAll('#results .result, #reader .reader-content'))el.dataset.stale='1';});}
async function freshRender(p,data){const first=data.results[0].chunk_id;
  await wait(async()=>(await p.locator('#results .result:not([data-stale])').first().getAttribute('data-chunk').catch(()=>null))===first
    &&await p.locator('#results .result[data-stale]').count()===0&&await p.locator('#reader .reader-content:not([data-stale])').count()===1);}
async function searchFor(a,q){
  await a.p.locator('#query').fill(q);await tagStale(a.p);
  const [resp]=await Promise.all([a.p.waitForResponse(r=>isSearch(r,q)),a.p.locator('#search-submit').click()]);
  assert.equal(resp.status(),200);const data=await resp.json();assert.ok(data.results.length>0,'no results for '+q);
  await freshRender(a.p,data);return data;
}
const verifying=p=>p.evaluate(()=>document.body.classList.contains('verifying'));
const gate=()=>{let open;const promise=new Promise(r=>open=r);return {promise,open};};
(async()=>{
 browser=await chromium.launch({channel:'msedge',headless:true});
 const owner=await actor('owner');
 await importDoc(owner,A,'redis-snapshot','By default Redis saves snapshots of the dataset on disk in a binary file called dump.rdb. <img src=x onerror="window.injected=1"> markup must stay plain text.');
 await importDoc(owner,A,'retention-guide','Backup retention is seven days. Older backups are deleted.');
 await owner.p.reload();await owner.p.locator('#workspace-body').waitFor();await wait(()=>owner.p.locator('.doc-row').count().then(n=>n>=2));
 const editor=await actor('editor'),viewer=await actor('viewer');

 await test('UX01-welcome',async()=>{
  // Welcome text sits outside #results/#reader, so the containers stay empty (no stale-content false positive).
  assert.equal(await editor.p.locator('.welcome').isVisible(),true);assert.equal(await editor.p.locator('.welcome .example').count(),3);
  assert.equal(await editor.p.locator('#results').innerText(),'');assert.equal(await editor.p.locator('#reader').innerText(),'');
  await searchFor(editor,'dump.rdb snapshot');assert.equal(await editor.p.locator('.welcome').isVisible(),false);
  await editor.p.locator('#workspace-select').selectOption(B);await wait(async()=>await editor.p.locator('#workspace-body').isVisible()&&await editor.p.locator('#current-role').innerText()==='只读');
  assert.equal(await editor.p.locator('#results').innerText(),'');assert.equal(await editor.p.locator('#reader').innerText(),'');assert.equal(await editor.p.locator('.welcome').isVisible(),true);
  await editor.p.locator('#workspace-select').selectOption(A);await wait(()=>editor.p.locator('#current-role').innerText().then(t=>t==='可编辑'));
 });

 await test('UX02-tab-return-keeps-work-and-locks-until-verified',async()=>{
  await searchFor(owner,'dump.rdb snapshot');const before=await owner.p.locator('#results').innerText();
  const g=gate();let held=0;
  await owner.p.route('**/api/workspaces',async route=>{held++;await g.promise;await route.continue().catch(()=>{});});
  await setHidden(owner.p,true);await setHidden(owner.p,false);
  await wait(()=>held>0);
  assert.equal(await verifying(owner.p),true);
  for(const id of ['#import-open','#search-submit','#translation-submit','#members-open'])assert.equal(await owner.p.locator(id).isDisabled(),true,id);
  assert.equal(await owner.p.locator('#query').inputValue(),'dump.rdb snapshot');assert.equal(await owner.p.locator('#results').innerText(),before);
  assert.equal(await owner.p.locator('#workspace-body').evaluate(el=>el.inert),true);assert.equal(await owner.p.locator('#documents').evaluate(el=>el.inert),true);
  g.open();await wait(async()=>!(await verifying(owner.p)));await owner.p.unroute('**/api/workspaces');
  assert.equal(await owner.p.locator('#import-open').isDisabled(),false);assert.equal(await owner.p.locator('#search-submit').isDisabled(),false);
  assert.equal(await owner.p.locator('#results').innerText(),before);assert.equal(await owner.p.locator('#query').inputValue(),'dump.rdb snapshot');
 });

 await test('UX03-refresh-started-before-return-cannot-unlock',async()=>{
  // Two returns in a row: the refresh started by the first return is still in flight at the second one,
  // so finishing it must not unlock; a new refresh must start right after it.
  const first=gate(),second=gate();let calls=0;
  await owner.p.route('**/api/workspaces',async route=>{const n=++calls;await (n===1?first.promise:second.promise);await route.continue().catch(()=>{});});
  await setHidden(owner.p,true);await setHidden(owner.p,false);await wait(()=>calls===1,8000);
  await setHidden(owner.p,true);await setHidden(owner.p,false);
  first.open();await wait(()=>calls===2,8000);
  assert.equal(await verifying(owner.p),true);
  second.open();await wait(async()=>!(await verifying(owner.p)));await owner.p.unroute('**/api/workspaces');
 });

 await test('UX04-removed-while-hidden-clears-on-return',async()=>{
  await searchFor(viewer,'backup retention');
  await setHidden(viewer.p,true);
  const r=await req(owner,'DELETE',scoped(A,'/members/'+users.viewer));assert.equal(r.status(),204);
  await setHidden(viewer.p,false);
  await wait(()=>viewer.p.locator('#no-workspace').isVisible(),8000);
  assert.equal(await viewer.p.locator('#results').innerText(),'');assert.equal(await viewer.p.locator('#reader').innerText(),'');
  assert.equal(await viewer.p.locator('#documents').innerText(),'');assert.equal(await viewer.p.locator('#query').inputValue(),'');
  const back=await req(owner,'POST',scoped(A,'/members'),{username:'ui-viewer',role:'viewer'});assert.equal(back.status(),201);
 });

 await test('UX05-network-down-on-return-blocks-writes-then-recovers',async()=>{
  await searchFor(owner,'dump.rdb snapshot');const before=await owner.p.locator('#results').innerText();
  await owner.p.route('**/api/workspaces',route=>route.abort('internetdisconnected'));
  await setHidden(owner.p,true);await setHidden(owner.p,false);
  await wait(()=>owner.p.locator('#page-error').innerText().then(t=>t.includes('网络连接中断')),8000);
  assert.equal(await owner.p.locator('#import-open').isDisabled(),true);assert.equal(await verifying(owner.p),true);
  await owner.p.unroute('**/api/workspaces');
  await wait(async()=>!(await verifying(owner.p))&&await owner.p.locator('#workspace-body').isVisible(),15000);
  assert.equal(await owner.p.locator('#import-open').isDisabled(),false);assert.equal(await owner.p.locator('#results').innerText(),before);
 });

 await test('LOCK01-late-search-neither-unlocks-nor-renders',async()=>{
  const log=r=>{const u=r.url();if(u.endsWith('/search'))mark(r.request?'response:search':'request:search',{query:JSON.parse((r.request?r.request():r).postData()||'{}').query});else if(u.endsWith('/api/workspaces'))mark(r.request?'response:workspaces':'request:workspaces');};
  owner.p.on('request',log);owner.p.on('response',log);
  try{
  const base=await searchFor(owner,'backup retention');mark('baseline-rendered',{first:base.results[0].chunk_id});
  const before=await owner.p.locator('#results').innerText();
  await owner.p.evaluate(()=>{for(const el of document.querySelectorAll('#results .result'))el.dataset.baseline='1';});
  const sg=gate(),wg=gate();let searches=0,checks=0;
  await owner.p.route('**/search',async route=>{if(++searches===1)await sg.promise;await route.continue().catch(()=>{});});
  await owner.p.locator('#query').fill('dump.rdb snapshot');await owner.p.locator('#search-submit').click();await wait(()=>searches===1);mark('late-search-held');
  await owner.p.route('**/api/workspaces',async route=>{checks++;await wg.promise;await route.continue().catch(()=>{});});
  await setHidden(owner.p,true);await setHidden(owner.p,false);await wait(()=>checks>0);mark('returned-check-held');
  sg.open();await owner.p.waitForFunction(()=>state.researchAfterCheck===true);mark('late-response-set-aside');
  assert.equal(await verifying(owner.p),true);assert.equal(await owner.p.locator('#search-submit').isDisabled(),true);
  // Unchanged during the check: same text AND the very same result nodes (nothing re-rendered).
  assert.equal(await owner.p.locator('#results').innerText(),before);
  assert.equal(await owner.p.locator('#results .result:not([data-baseline])').count(),0);
  await tagStale(owner.p);
  const [again]=await Promise.all([owner.p.waitForResponse(r=>isSearch(r,'dump.rdb snapshot')),(async()=>{wg.open();mark('check-released');})()]);
  mark('research-response');await wait(async()=>!(await verifying(owner.p)));
  await freshRender(owner.p,await again.json());mark('research-rendered');   // the re-search itself was rendered, not a leftover
  assert.equal(searches,2);assert.equal(await owner.p.locator('#search-submit').isDisabled(),false);
  await owner.p.unroute('**/search');await owner.p.unroute('**/api/workspaces');
  }finally{owner.p.off('request',log);owner.p.off('response',log);save();}
 });

 await test('LOCK02-dialog-buttons-follow-the-lock',async()=>{
  await owner.p.locator('#members-open').click();await owner.p.locator('#members-dialog').waitFor();await owner.p.locator('.member-row').first().waitFor();
  const buttons=owner.p.locator('#members-list button, #members-list select, #member-submit');
  const enabled=async want=>{for(const el of await buttons.all())assert.equal(await el.isDisabled(),!want);};
  await enabled(true);
  const wg=gate();let checks=0;await owner.p.route('**/api/workspaces',async route=>{checks++;await wg.promise;await route.continue().catch(()=>{});});
  await setHidden(owner.p,true);await setHidden(owner.p,false);await wait(()=>checks>0);
  await enabled(false);
  wg.open();await wait(async()=>!(await verifying(owner.p)));await enabled(true);
  await owner.p.unroute('**/api/workspaces');await owner.p.locator('[data-close=members-dialog]').click();await owner.p.locator('#members-dialog').waitFor({state:'hidden'});
 });

 await test('LOCK03-keyboard-cannot-reach-locked-areas',async()=>{
  const wg=gate();let checks=0;await owner.p.route('**/api/workspaces',async route=>{checks++;await wg.promise;await route.continue().catch(()=>{});});
  await setHidden(owner.p,true);await setHidden(owner.p,false);await wait(()=>checks>0);
  const canFocus=sel=>owner.p.locator(sel).first().evaluate(el=>{el.focus();return document.activeElement===el;});
  for(const sel of ['#query','.doc-row','#results .result'])assert.equal(await canFocus(sel),false,sel);
  wg.open();await wait(async()=>!(await verifying(owner.p)));
  assert.equal(await canFocus('#query'),true);await owner.p.unroute('**/api/workspaces');
 });

 await test('TR01-corrected-translation-keeps-language-and-method',async()=>{
  const sent=[];await owner.p.route('**/search',route=>{sent.push(JSON.parse(route.request().postData()));return route.fallback();});
  await owner.p.locator('#language').selectOption('auto');await owner.p.locator('#method').selectOption('auto');
  await owner.p.locator('#query').fill('备份保留策略');await owner.p.locator('#search-submit').click();
  await wait(()=>owner.p.locator('#translation').isVisible());assert.equal(sent.at(-1).translation,undefined);
  await owner.p.locator('#translation-input').fill('backup retention seven days');
  const [resp]=await Promise.all([owner.p.waitForResponse(r=>isSearch(r,'备份保留策略',b=>b.translation==='backup retention seven days'&&!b.document_id)),owner.p.locator('#translation-submit').click()]);
  const body=sent.at(-1),data=await resp.json();
  assert.deepEqual([body.query,body.language,body.method,body.translation],['备份保留策略','auto','auto','backup retention seven days']);
  assert.equal(resp.status(),200);assert.equal(data.language,'zh');assert.equal(data.method,'bm25');assert.equal(data.translated_query,'backup retention seven days');
  await wait(()=>owner.p.locator('#translation-hint').innerText().then(t=>t.includes('匹配方式没变')));
  assert.equal(await owner.p.locator('#query').inputValue(),'备份保留策略');assert.equal(await owner.p.locator('#translation-input').inputValue(),'backup retention seven days');
  const [scoped1]=await Promise.all([owner.p.waitForResponse(r=>isSearch(r,'备份保留策略',b=>!!b.document_id)),owner.p.locator('.doc-row').filter({hasText:'retention-guide'}).click()]);assert.equal(scoped1.status(),200);
  assert.equal(sent.at(-1).translation,'backup retention seven days');
  const [all]=await Promise.all([owner.p.waitForResponse(r=>isSearch(r,'备份保留策略',b=>!b.document_id)),owner.p.locator('#all-documents').click()]);assert.equal(all.status(),200);
  await owner.p.locator('#query').fill('另一个问题 retention');
  const [fresh]=await Promise.all([owner.p.waitForResponse(r=>isSearch(r,'另一个问题 retention')),owner.p.locator('#search-submit').click()]);assert.equal(fresh.status(),200);
  assert.equal(sent.at(-1).query,'另一个问题 retention');assert.equal(sent.at(-1).translation,undefined);
  await owner.p.unroute('**/search');
 });

 await test('HL01-highlight-is-text-only-and-matches-backend-terms',async()=>{
  await owner.p.locator('#language').selectOption('en');
  const data=await searchFor(owner,'what is the dump.rdb onerror file');await owner.p.locator('#results .result mark').first().waitFor();
  const terms=new Set(data.results.flatMap(r=>r.matched_terms));
  const marks=await owner.p.locator('#results mark, #reader mark').allInnerTexts();
  assert.ok(marks.length>0);for(const m of marks)assert.ok(terms.has(m.toLowerCase()),m);
  for(const stop of ['what','is','the'])assert.ok(!marks.map(m=>m.toLowerCase()).includes(stop),stop);
  assert.equal(await owner.p.locator('#results img, #reader img').count(),0);
  assert.equal(await owner.p.evaluate(()=>window.injected),undefined);
  assert.ok((await owner.p.locator('#results').innerText()).includes('<img src=x'));
  await owner.p.locator('#language').selectOption('auto');
 });

 await test('END',async()=>{assert.equal(errors.length,0,JSON.stringify(errors));});
})().catch(e=>{console.error(e.message.replaceAll(credentials.password,'[REDACTED]'));process.exitCode=1;}).finally(async()=>{for(const c of contexts)await c.close().catch(()=>{});if(browser)await browser.close();save();});
