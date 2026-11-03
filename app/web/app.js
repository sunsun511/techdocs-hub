'use strict';
// Credentials stay in HttpOnly cookies; CSRF and view state live only in memory.
const $ = id => document.getElementById(id);
const state = {user:null, csrf:null, ws:null, role:null, epoch:0, requests:new Set(), seq:{}, busy:false, online:false,
  documents:[], document:null, version:'', generation:null, lastQuery:'', selectedChunk:null, timer:null, polling:false, importKey:null, override:null, docSignature:null, terms:{}, verifying:false, returnSeq:0, recheck:false, researchAfterCheck:false};
const statuses = {queued:'等待索引',running:'正在索引',succeeded:'已完成',failed:'索引失败',superseded:'已被替代',cancelled:'已取消',ready:'已就绪'};
const roles = {owner:'管理员',editor:'可编辑',viewer:'只读'};
const memberStatus = {active:'正常',disabled:'已停用'};
const node = (tag,text,cls) => {const el=document.createElement(tag); if(text!==undefined)el.textContent=text; if(cls)el.className=cls;return el;};
const clear = id => $(id).replaceChildren();
const context = () => ({epoch:state.epoch,ws:state.ws});
const current = ctx => ctx.epoch===state.epoch && ctx.ws===state.ws;
const cancelled = () => new DOMException('Stale view','AbortError');
const canWrite = () => state.online && !state.verifying && ['owner','editor'].includes(state.role);
// Common English words are matched by the backend but are noise when highlighted.
const stopWords = new Set('a an and are as at be by can do does for from how i if in is it of on or that the this to was what when where which who why will with you your'.split(' '));
function keyTerms(terms){return (terms||[]).filter(t=>/^[a-z0-9_]+$/.test(t)&&!stopWords.has(t));}
function termPattern(terms){const list=keyTerms(terms);return list.length?new RegExp(`(?<![A-Za-z0-9_])(${list.join('|')})(?![A-Za-z0-9_])`,'gi'):null;}
// Builds text + <mark> nodes only; never parses document text as HTML.
function highlighted(tag,text,terms,cls){
  const el=node(tag,undefined,cls);const re=termPattern(terms);if(!re){el.textContent=text;return el;}
  let last=0;for(const m of text.matchAll(re)){if(m.index>last)el.append(text.slice(last,m.index));el.append(node('mark',m[0]));last=m.index+m[0].length;}
  if(last<text.length)el.append(text.slice(last));return el;
}
// Shows the part of a long passage around the first matched word instead of always the beginning.
function snippet(text,terms,size=360){
  if(text.length<=size)return text;const re=termPattern(terms);const hit=re?text.search(re):-1;
  let start=hit>120?text.lastIndexOf(' ',hit-100):0;if(start<0)start=0;const end=Math.min(text.length,start+size);
  return (start>0?'…':'')+text.slice(start,end).trim()+(end<text.length?'…':'');
}
// The welcome text and examples live next to #results/#reader and are shown by CSS only while those are empty,
// so the containers themselves stay empty after a workspace switch (no stale content).
function welcome(){clear('results');clear('reader');}
function setCompact(on){document.querySelector('.search-header').classList.toggle('compact',on);}
function hideTranslation(){$('translation').hidden=true;$('translation-input').value='';$('translation-hint').textContent='';}
function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,3500);}
function invalidate(){state.busy=false;state.epoch++;for(const c of state.requests)c.abort();state.requests.clear();state.seq={};clearTimeout(state.timer);}
function closeDialogs(){for(const d of document.querySelectorAll('dialog[open]'))d.close();}
function clearWorkspace(){
  closeDialogs();for(const id of ['documents','versions-list','tasks-list','members-list','search-notices'])clear(id);hideTranslation();welcome();setCompact(false);
  state.documents=[];state.document=null;state.version='';state.generation=null;state.lastQuery='';state.selectedChunk=null;state.importKey=null;state.membersSignature=null;state.override=null;state.docSignature=null;state.terms={};state.researchAfterCheck=false;
  $('import-form').reset();$('member-form').reset();$('query').value='';$('document-count').textContent='0';$('task-badge').textContent='';
  $('version-filter').replaceChildren(new Option('当前版本',''));$('version-filter-label').hidden=true;$('versions-open').disabled=true;$('versions-open').hidden=true;$('scope-clear').hidden=true;
  $('scope-label').textContent='全部文档';$('all-documents').classList.add('selected');$('result-meta').textContent='等待搜索';$('toast').hidden=true;
}
function memberControls(){
  const disabled=state.busy||!state.online||state.verifying||state.role!=='owner';
  for(const el of $('members-list').querySelectorAll('select,button'))el.disabled=disabled;
}
function controls(){
  memberControls();
  const available=!!state.ws&&state.online;
  $('current-role').textContent=roles[state.role]||'';$('workspace-select').disabled=state.busy;
  $('import-open').hidden=!['owner','editor'].includes(state.role);$('import-open').disabled=!canWrite()||state.busy;
  $('add-version').hidden=$('delete-document').hidden=!['owner','editor'].includes(state.role);
  $('add-version').disabled=$('delete-document').disabled=!canWrite()||state.busy;
  $('members-open').hidden=state.role!=='owner';$('members-open').disabled=!available||state.busy;
  $('member-submit').disabled=!available||state.busy;$('import-submit').disabled=!canWrite()||state.busy;
  $('search-submit').disabled=!available;$('tasks-open').disabled=!available;$('all-documents').disabled=!available;
  $('versions-open').disabled=!available||!state.document;$('versions-open').hidden=!state.document;$('scope-clear').hidden=!state.document;$('translation-submit').disabled=!available;$('workspace-body').hidden=!available;
  $('no-workspace').hidden=!!state.ws;$('logout').disabled=$('logout-all').disabled=state.busy;
  // While permissions are re-checked after returning to the tab, the old view stays but cannot be used.
  document.body.classList.toggle('verifying',state.verifying);$('workspace-body').setAttribute('aria-busy',String(state.verifying));
  if(state.verifying){for(const id of ['search-submit','translation-submit','members-open','member-submit','tasks-open','all-documents','versions-open'])$(id).disabled=true;}
  // Buttons rendered inside dialogs (set version, retry task) follow the same write rule.
  for(const el of document.querySelectorAll('.write-action'))el.disabled=!canWrite()||state.busy;
  // inert blocks mouse, keyboard and focus, not just pointer clicks.
  $('workspace-body').inert=state.verifying;$('documents').inert=state.verifying;$('all-documents').inert=state.verifying;
}
function signedOut(message=''){
  invalidate();clearWorkspace();state.user=null;state.csrf=null;state.ws=null;state.role=null;state.online=false;state.busy=false;state.verifying=false;state.recheck=false;state.researchAfterCheck=false;
  $('app-shell').hidden=true;$('login-view').hidden=false;$('password').value='';$('login-error').textContent=message;$('login-submit').disabled=false;
  $('workspace-select').replaceChildren();$('current-user').textContent='';$('user-menu').open=false;controls();
}
async function api(path,options={},ctx=context(),key=null){
  if(!current(ctx))throw cancelled();
  const serial=key?(state.seq[key]=(state.seq[key]||0)+1):null;
  const controller=new AbortController();state.requests.add(controller);const timeout=setTimeout(()=>controller.abort('timeout'),30000);
  let response;
  try{
    const headers={'Content-Type':'application/json',...(state.csrf?{'X-CSRF-Token':state.csrf}:{}),...(options.headers||{})};
    response=await fetch(path,{...options,headers,credentials:'same-origin',cache:'no-store',signal:controller.signal});
    const text=response.status===204?'':await response.text();
    if(!current(ctx)||(key&&state.seq[key]!==serial))throw cancelled();
    let data=null;try{data=text?JSON.parse(text):null;}catch{if(response.ok)throw new Error('服务返回格式无效');}
    if(!response.ok){
      const detail=typeof data?.detail==='string'?data.detail:`请求失败 (${response.status})`;
      const error=new Error(response.status===429?'登录尝试过多，请稍后再试':detail);error.status=response.status;
      if(response.status===401&&!path.endsWith('/login'))signedOut(state.user?'会话已失效，请重新登录':'');
      throw error;
    }
    return data;
  }catch(error){
    if(!current(ctx)||(key&&state.seq[key]!==serial))throw cancelled();
    if(controller.signal.reason==='timeout')throw new Error('请求超时，结果可能未知；请刷新原工作区状态后再决定是否重试');
    if(error instanceof TypeError){state.online=false;controls();throw new Error('网络连接中断，写入已暂停；正在等待重新核验');}
    throw error;
  }finally{clearTimeout(timeout);state.requests.delete(controller);}
}
const scoped=(path,options={},ctx=context(),key=null)=>{if(!ctx.ws)throw new Error('请选择工作区');return api(`/api/workspaces/${encodeURIComponent(ctx.ws)}${path}`,options,ctx,key);};
function errorMessage(error,ctx,target='page-error'){
  if(error.name==='AbortError'||!current(ctx))return;
  $(target).textContent=error.message;
  if([403,404].includes(error.status)&&state.user){invalidate();clearWorkspace();state.online=false;controls();void recover();}
}
function act(fn,target='page-error'){return async event=>{const ctx=context();try{await fn(event,ctx);}catch(error){errorMessage(error,ctx,target);}};}
function schedule(){clearTimeout(state.timer);if(state.user)state.timer=setTimeout(()=>void refresh(),4000);}
async function chooseWorkspace(id){
  if(state.busy)return;
  if($('import-dialog').open&&!confirm('切换工作区将丢弃未提交内容，继续？')){$('workspace-select').value=state.ws||'';return;}
  invalidate();clearWorkspace();state.ws=id||null;state.role=null;state.online=false;$('workspace-select').value=id||'';controls();await refresh();
}
async function refresh(){
  if(state.polling){schedule();return;}if(!state.user||document.hidden){schedule();return;}
  state.polling=true;const ctx=context();const returnSeq=state.returnSeq;
  try{
    const list=await api('/api/workspaces',{},ctx,'workspaces');if(!current(ctx))return;
    $('workspace-select').replaceChildren(...list.workspaces.map(w=>new Option(w.name,w.id)));
    let chosen=list.workspaces.find(w=>w.id===state.ws);
    if(!chosen){
      invalidate();clearWorkspace();state.ws=list.workspaces[0]?.id||null;state.role=null;state.online=false;controls();
      if(state.ws){state.polling=false;await refresh();}return;
    }
    $('workspace-select').value=state.ws;
    if(state.role!==null&&state.role!==chosen.role){invalidate();clearWorkspace();state.role=chosen.role;state.online=false;controls();state.polling=false;await refresh();return;}
    state.role=chosen.role;
    const [health,docs,tasks]=await Promise.all([scoped('/summary',{},ctx),scoped('/documents',{},ctx),scoped('/tasks',{},ctx)]);
    if(!current(ctx))return;
    const changed=state.generation!==null&&state.generation!==health.generation;
    state.generation=health.generation;state.documents=docs.documents;state.online=true;if(returnSeq===state.returnSeq)state.verifying=false;$('page-error').textContent='';$('connection').textContent=`${health.ready_documents} 份文档可搜索`;
    renderDocuments();renderTasks(tasks.tasks);controls();
    if(state.document){const found=docs.documents.find(d=>d.id===state.document.id);state.document=found||null;if(!found)resetResults();}
    if($('members-dialog').open&&state.role==='owner')await renderMembers();
    if($('versions-dialog').open&&state.document)await renderVersions();
    const deferred=state.researchAfterCheck&&!state.verifying;if(deferred)state.researchAfterCheck=false;
    if(changed){resetResults();if(state.lastQuery)await search();}else if(deferred&&state.lastQuery)await search();
  }catch(error){errorMessage(error,ctx);}
  finally{state.polling=false;if(state.recheck){state.recheck=false;void refresh();}else schedule();}
}
async function recover(){try{await refresh();}catch{schedule();}}
function resetResults(){clear('search-notices');hideTranslation();state.selectedChunk=null;state.terms={};
  if(state.lastQuery){clear('results');clear('reader');$('results').append(node('p','搜索范围变了，正在重新搜索…','muted pad'));$('result-meta').textContent='范围已更新';}
  else{welcome();$('result-meta').textContent='等待搜索';}}
function renderDocuments(){
  const signature=JSON.stringify([state.document?.id||null,state.documents]);if(signature===state.docSignature)return;state.docSignature=signature;
  clear('documents');$('all-documents').classList.toggle('selected',!state.document);$('document-count').textContent=String(state.documents.length);
  // Group by product prefix ("Docker · Volumes" -> Docker / Volumes) so the library reads like an index.
  const groups=new Map();
  for(const doc of state.documents){const title=doc.title||doc.key;const cut=title.indexOf(' · ');const group=cut>0?title.slice(0,cut):'其他文档';
    if(!groups.has(group))groups.set(group,[]);groups.get(group).push([doc,cut>0?title.slice(cut+3):title,title]);}
  const showGroups=groups.size>1||!groups.has('其他文档');
  for(const [group,items] of groups){
    if(showGroups)$('documents').append(node('div',group,'doc-group'));
    for(const [doc,name,title] of items){const b=node('button',undefined,'doc-row'+(state.document?.id===doc.id?' selected':''));b.dataset.documentId=doc.id;b.title=title;
      const meta=node('small');if(doc.version_label){meta.append(node('span',doc.version_label,'doc-version'),node('span',`${doc.chunk_count} 个片段`));}else meta.append(node('span',statuses[doc.task_state]||'等待索引'));
      b.append(node('strong',name),meta);b.onclick=act(()=>selectDocument(doc));$('documents').append(b);}
  }
  if(!state.documents.length)$('documents').append(node('p','文档库为空。','muted'));
}
async function selectDocument(doc){
  state.document=doc;state.version='';state.seq.versions=(state.seq.versions||0)+1;state.seq.reader=(state.seq.reader||0)+1;state.seq.search=(state.seq.search||0)+1;
  $('version-filter-label').hidden=!doc;$('version-filter').replaceChildren(new Option('当前版本',''));$('scope-label').textContent=doc?(doc.title||doc.key):'全部文档';resetResults();renderDocuments();controls();
  if(doc){const ctx=context();const data=await scoped(`/documents/${doc.id}/versions`,{},ctx,'versions');if(state.document?.id!==doc.id)return;for(const v of data.versions.filter(v=>v.status==='ready'))$('version-filter').append(new Option(v.label,v.id));}
  if(state.lastQuery)await search();
}
// opts.fresh: a new question typed by the user; opts.lexical: the user's corrected English wording.
async function search(opts={}){
  const query=$('query').value.trim();if(!query||!state.online||state.verifying)return;
  if(opts.fresh)state.override=null;
  if(opts.lexical)state.override={source:query,text:opts.lexical};
  // A corrected translation applies only where the backend has a translation step (Chinese, not 'by meaning').
  if(state.override&&(state.override.source!==query||$('language').value==='en'||$('method').value==='dense'))state.override=null;
  const override=state.override;
  state.lastQuery=query;const ctx=context();const sentAt=state.returnSeq;state.seq.reader=(state.seq.reader||0)+1;state.selectedChunk=null;const searchSerial=(state.seq.search||0)+1;$('search-submit').disabled=true;$('translation-submit').disabled=true;
  try{
    const body={query,language:$('language').value,method:$('method').value,top_k:5,document_id:state.document?.id||null,version_id:state.version||null};if(override)body.translation=override.text;
    const data=await scoped('/search',{method:'POST',body:JSON.stringify(body)},ctx,'search');
    // Sent before the user came back to this tab: keep the old view and search again once permissions are confirmed.
    if(sentAt!==state.returnSeq){state.researchAfterCheck=true;return;}
    clear('results');clear('reader');clear('search-notices');setCompact(true);state.terms={};
    $('result-meta').textContent=`${data.results.length} 条结果 · ${Math.round(data.elapsed_ms)} ms`;
    for(const warning of data.warnings)$('search-notices').append(node('div',warning));
    const english=data.translated_query;
    $('translation').hidden=!english;
    if(english){$('translation-input').value=english;$('translation-hint').textContent=override?`已按你改过的英文搜索，匹配方式没变。原问题：${query}`:'机器翻译可能把技术词译错。不对的话直接在框里改，再点"重新搜"。';}
    for(const [rank,result] of data.results.entries()){
      state.terms[result.chunk_id]=result.matched_terms;
      const b=node('button',undefined,'result');b.dataset.chunk=result.chunk_id;
      b.append(node('span',String(rank+1),'rank'));
      const top=node('div',undefined,'result-top');top.append(node('strong',result.title),node('span',result.version_label,'tag'));b.append(top);
      const heading=(result.heading||'').trim();if(heading&&![result.title,result.document_key].some(x=>x&&x.toLowerCase()===heading.toLowerCase()))b.append(node('div',heading,'breadcrumb'));
      b.append(highlighted('p',snippet(result.text,result.matched_terms),result.matched_terms));
      const words=keyTerms(result.matched_terms);const foot=node('div',undefined,'result-foot');
      if(words.length)foot.append(node('span','命中：'+words.join('、'),'hits'));
      if(result.match_hint==='low')foot.append(node('span','命中的词较少，请仔细核对','weak'));
      if(foot.childNodes.length)b.append(foot);
      b.onclick=act(()=>readPassage(result.chunk_id));$('results').append(b);
    }
    if(data.results.length)await readPassage(data.results[0].chunk_id);
    else{const empty=node('div',undefined,'no-results');empty.append(node('strong','这个范围里没有找到匹配的内容'),node('p',english?'可以改一下上面的英文检索词，或者换成文档里会出现的英文术语再试。':'试试更短的关键词，或者换成文档里会出现的英文术语。'));$('results').append(empty);}
  }finally{if(current(ctx)&&state.seq.search===searchSerial)controls();}
}
async function readPassage(cid){
  const ctx=context();const sentAt=state.returnSeq;state.selectedChunk=cid;
  for(const el of $('results').querySelectorAll('.result'))el.classList.toggle('selected',el.dataset.chunk===cid);
  const data=await scoped(`/passages/${encodeURIComponent(cid)}`,{},ctx,'reader');if(state.selectedChunk!==cid||sentAt!==state.returnSeq)return;clear('reader');
  const terms=state.terms[cid]||[];
  const content=node('div',undefined,'reader-content');const head=node('div',undefined,'reader-head');const top=node('div',undefined,'reader-top');top.append(node('span',data.version_label,'tag'));head.append(top,node('h3',data.title,'reader-title'),node('div','出处：'+data.heading,'breadcrumb'));content.append(head);
  try{const url=new URL(data.source_url);if(['http:','https:'].includes(url.protocol)){const a=node('a','打开官网原文','reader-source');a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';top.append(a);}}catch{/* Missing/unsafe source links are not rendered. */}
  let focus=null;for(const p of data.neighbors){const el=highlighted('pre',p.text,terms,'reader-text'+(p.id===cid?' focus':''));if(p.id===cid)focus=el;content.append(el);}
  $('reader').append(content);if(focus&&content.scrollHeight>content.clientHeight)content.scrollTop=Math.max(0,focus.offsetTop-head.offsetHeight-16);
}
async function mutate(path,method,body,target='page-error',headers={}){
  if(state.busy)throw cancelled();if(!state.online||state.verifying)throw new Error('请等待连接与权限重新核验');const ctx=context();state.busy=true;controls();
  try{return await scoped(path,{method,headers,...(body!==undefined?{body:JSON.stringify(body)}:{body:JSON.stringify({})})},ctx);}
  catch(error){errorMessage(error,ctx,target);throw error;}
  finally{if(current(ctx)){state.busy=false;controls();}}
}
async function renderVersions(){
  const doc=state.document;if(!doc)return;const ctx=context();const data=await scoped(`/documents/${doc.id}/versions`,{},ctx,'versions');if(state.document?.id!==doc.id)return;clear('versions-list');$('version-title').textContent=`${doc.title||doc.key} · 版本`;
  for(const v of data.versions){const row=node('div',undefined,'version-row');const info=node('div');info.append(node('strong',`${v.label}${data.document.active_version_id===v.id?' · 当前':''}`),node('small',`${statuses[v.status]||v.status} · ${v.chunk_count} 个片段`));if(v.error)info.append(node('p',v.error,'error'));row.append(info);
    if(canWrite()&&v.status==='ready'&&data.document.active_version_id!==v.id){const b=node('button','设为当前','write-action');b.disabled=!canWrite()||state.busy;b.onclick=act(async()=>{await mutate(`/documents/${doc.id}/active-version`,'POST',{version_id:v.id},'version-error');await refresh();},'version-error');row.append(b);}$('versions-list').append(row);
  }
}
function renderTasks(tasks){
  clear('tasks-list');$('task-badge').textContent=String(tasks.filter(t=>['queued','running'].includes(t.state)).length||'');
  for(const t of tasks){const row=node('div',undefined,'task-row');row.dataset.taskId=t.id;row.append(node('strong',`${t.title} · ${t.version_label}`),node('small',statuses[t.state]||t.state));if(t.error)row.append(node('p',t.error,'error'));
    if(t.state==='failed'&&canWrite()){const b=node('button','重试','write-action');b.disabled=!canWrite()||state.busy;b.onclick=act(async()=>{await mutate(`/tasks/${t.id}/retry`,'POST',{},'tasks-error');await refresh();},'tasks-error');row.append(b);}$('tasks-list').append(row);
  }if(!tasks.length)$('tasks-list').append(node('p','暂无索引任务。'));
}
async function renderMembers(){
  if(state.role!=='owner')return;const data=await scoped('/members',{},context(),'members');const signature=JSON.stringify(data);if(state.membersSignature===signature){memberControls();return;}state.membersSignature=signature;clear('members-list');
  for(const m of data.members){const row=node('div',undefined,'member-row');row.dataset.userId=m.user_id;row.append(node('strong',`${m.username} · ${m.display_name}${m.status==='active'?'':' · '+(memberStatus[m.status]||m.status)}`));
    const select=node('select');select.setAttribute('aria-label',`${m.username} 角色`);for(const r of ['owner','editor','viewer'])select.append(new Option(roles[r],r));select.value=m.role;select.disabled=state.busy;
    const change=node('button','保存角色');change.onclick=act(async()=>{if(!confirm(`将 ${m.username} 改为「${roles[select.value]}」？`))return;await mutate(`/members/${m.user_id}`,'PUT',{role:select.value},'member-error');await refresh();},'member-error');
    const remove=node('button','移除');remove.onclick=act(async()=>{if(!confirm(`从当前工作区移除 ${m.username}？`))return;await mutate(`/members/${m.user_id}`,'DELETE',{},'member-error');await refresh();},'member-error');row.append(select,change,remove);$('members-list').append(row);
  }
  memberControls();
}
$('login-form').onsubmit=act(async e=>{
  e.preventDefault();invalidate();clearWorkspace();state.ws=null;state.csrf=null;const ctx=context();$('login-submit').disabled=true;$('login-error').textContent='';
  let password=$('password').value;$('password').value='';
  try{const data=await api('/api/auth/login',{method:'POST',body:JSON.stringify({username:$('username').value,password})},ctx);state.user=data.user;state.csrf=data.csrf_token;$('login-view').hidden=true;$('app-shell').hidden=false;$('current-user').textContent=data.user.username;await refresh();}
  catch(error){if(current(ctx))$('login-error').textContent=error.message;}
  finally{password=null;if(current(ctx))$('login-submit').disabled=false;}
},'login-error');
async function logout(all){if(all&&!confirm('在所有设备上退出？其他浏览器和窗口也需要重新登录。'))return;const ctx=context();await api('/api/auth/'+(all?'logout-all':'logout'),{method:'POST',body:'{}'},ctx);if(current(ctx))signedOut();}
$('logout').onclick=act(()=>logout(false));$('logout-all').onclick=act(()=>logout(true));
$('workspace-select').onchange=act(e=>chooseWorkspace(e.target.value));
$('search-form').onsubmit=act(e=>{e.preventDefault();return search({fresh:true});});
$('translation').onsubmit=act(e=>{e.preventDefault();const text=$('translation-input').value.trim();if(!text)throw new Error('请先填写用来搜索的英文');return search({lexical:text});});
$('scope-clear').onclick=act(()=>selectDocument(null));
for(const b of document.querySelectorAll('.example'))b.onclick=act(()=>{$('query').value=b.dataset.query;return search({fresh:true});});
document.addEventListener('click',e=>{if($('user-menu').open&&!$('user-menu').contains(e.target))$('user-menu').open=false;});
$('all-documents').onclick=act(()=>selectDocument(null));
$('version-filter').onchange=act(()=>{state.version=$('version-filter').value;resetResults();return state.lastQuery?search():undefined;});
for(const b of document.querySelectorAll('[data-close]'))b.onclick=()=>{if(!state.busy)$(b.dataset.close).close();};
for(const d of document.querySelectorAll('dialog'))d.addEventListener('cancel',e=>{if(state.busy)e.preventDefault();});
function openImport(doc=null){if(!canWrite())return;$('import-form').reset();state.importKey=crypto.randomUUID();$('import-error').textContent='';if(doc){const f=$('import-form');f.elements.key.value=doc.key;f.elements.title.value=doc.title||doc.key;f.elements.version_label.value='';}$('import-dialog').showModal();}
$('import-open').onclick=()=>openImport();
$('file').onchange=act(async()=>{const f=$('file').files[0];if(!f)return;if(f.size>1000000)throw new Error('文件超过1MB');const ctx=context();const content=await f.text();if(!current(ctx))return;const form=$('import-form');form.elements.content.value=content;form.elements.title.value=f.name;form.elements.key.value=f.name.replace(/[^a-zA-Z0-9._-]/g,'-').replace(/^[^a-zA-Z0-9]+/,'')||'document';form.elements.format.value=/\.html?$/i.test(f.name)?'html':/\.txt$/i.test(f.name)?'text':'markdown';},'import-error');
$('import-form').onsubmit=act(async e=>{e.preventDefault();const payload=Object.fromEntries(new FormData(e.target));const result=await mutate('/documents','POST',payload,'import-error',{'Idempotency-Key':state.importKey});$('import-dialog').close();toast(result.duplicate?'该版本已存在':'文档已接收，等待索引完成');await refresh();},'import-error');
$('versions-open').onclick=act(async()=>{$('version-error').textContent='';await renderVersions();$('versions-dialog').showModal();},'version-error');
$('tasks-open').onclick=act(async()=>{$('tasks-error').textContent='';$('tasks-dialog').showModal();await refresh();},'tasks-error');
$('members-open').onclick=act(async()=>{$('member-error').textContent='';await renderMembers();$('members-dialog').showModal();},'member-error');
$('member-form').onsubmit=act(async e=>{e.preventDefault();await mutate('/members','POST',{username:$('member-username').value,role:$('member-role').value},'member-error');$('member-form').reset();await renderMembers();},'member-error');
$('add-version').onclick=()=>{const doc=state.document;$('versions-dialog').close();openImport(doc);};
$('delete-document').onclick=act(async()=>{if(!state.document||!confirm('删除此文档及全部版本的搜索索引？'))return;await mutate(`/documents/${state.document.id}`,'DELETE',{},'version-error');$('versions-dialog').close();state.lastQuery='';await selectDocument(null);await refresh();},'version-error');
// Polling pauses while the tab is hidden. On return, permissions are re-checked immediately;
// refresh() still clears the view if the workspace or role changed, otherwise the user's work stays.
document.addEventListener('visibilitychange',()=>{if(document.hidden){clearTimeout(state.timer);return;}if(!state.user)return;
  // A refresh already in flight started before the return, so it cannot end verification; queue a new one right after it.
  clearTimeout(state.timer);state.returnSeq++;state.verifying=true;$('connection').textContent='正在重新确认权限…';controls();if(state.polling)state.recheck=true;else void refresh();});
(async()=>{const ctx=context();try{const data=await api('/api/auth/me',{},ctx);state.user=data.user;state.csrf=data.csrf_token;$('current-user').textContent=data.user.username;$('login-view').hidden=true;$('app-shell').hidden=false;await refresh();}catch(error){if(error.name!=='AbortError')signedOut(error.status===401?'':error.message);}})();
