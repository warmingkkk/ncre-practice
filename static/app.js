document.querySelectorAll('[data-confirm]').forEach(f=>f.addEventListener('submit',e=>{if(!confirm(f.dataset.confirm))e.preventDefault()}));
document.getElementById('print')?.addEventListener('click',()=>window.print());
const category=document.getElementById('publish-category');
category?.addEventListener('change',()=>document.querySelectorAll('[data-category]').forEach(el=>el.hidden=!!category.value&&el.dataset.category!==category.value));
const editSelection=document.getElementById('edit-selection');
if(editSelection){document.querySelectorAll('#task-edit-form input[name="questions"]').forEach(input=>input.addEventListener('change',()=>{editSelection.value='manual'}));document.getElementById('edit-count')?.addEventListener('input',()=>{editSelection.value='random'});}
const form=document.getElementById('answer-form');
if(form?.dataset.active==='1'){
 let pending=Promise.resolve(),submitted=false,saveTimer;
 const status=document.getElementById('save-status');
 const save=()=>{
  const data=new FormData(form);data.set('save','1');
  pending=pending.catch(()=>{}).then(async()=>{const r=await fetch(location.pathname,{method:'POST',body:data});if(!r.ok)throw Error();status.textContent='作答已保存';}).catch(()=>{status.textContent='保存失败，请检查网络并保持页面打开';});
  return pending;
 };
 form.addEventListener('change',e=>{document.querySelector(`[data-question="${e.target.name}"]`)?.classList.add('answered');status.textContent='正在保存…';clearTimeout(saveTimer);saveTimer=setTimeout(save,700);});
 form.addEventListener('submit',async e=>{if(submitted)return;e.preventDefault();if(!confirm('确认提交？未作答题目计为错误。'))return;submitted=true;clearTimeout(saveTimer);await pending;form.submit();});
 const timer=document.querySelector('[data-deadline]');
 if(timer){const tick=()=>{const remaining=Math.max(0,Math.ceil(Number(timer.dataset.deadline)-Date.now()/1000));timer.textContent=`剩余 ${Math.floor(remaining/60)}:${String(remaining%60).padStart(2,'0')}`;if(!remaining&&!submitted){submitted=true;form.submit();}};tick();setInterval(tick,1000);}
}
const table=document.querySelector('[data-poll]');
if(table){setInterval(async()=>{try{const response=await fetch(table.dataset.poll);if(!response.ok)throw Error();const data=await response.json();const body=document.createElement('tbody');for(const r of data){const row=body.insertRow();for(const value of [r.username,r.name,r.attempts,r.best??'—',r.status??'未作答'])row.insertCell().textContent=value;}table.tBodies[0].replaceWith(body);document.getElementById('poll-status').textContent='已更新 · '+new Date().toLocaleTimeString();}catch{document.getElementById('poll-status').textContent='更新失败，10秒后重试';}},10000);}

// Verify the current browser session before sending credentials or creating a class.
document.querySelectorAll('[data-session-form]').forEach(f=>{
 let busy=false;
 const message=f.querySelector('[data-session-message]');
 f.addEventListener('submit',async e=>{
  e.preventDefault();if(busy)return;busy=true;
  const button=f.querySelector('button[type="submit"],button:not([type])');
  const label=button.textContent;button.disabled=true;button.textContent='正在提交…';
  try{
   message.textContent='正在确认登录状态…';
   const r=await fetch('/session-state',{credentials:'same-origin',cache:'no-store',headers:{'Accept':'application/json'},signal:AbortSignal.timeout(10000)});
   if(!r.ok)throw Error('暂时无法确认登录状态，请稍后重试。');
   const state=await r.json();
   if(!state.cookie_ok&&state.secure&&location.protocol==='http:'){const target=new URL(location.href);target.protocol='https:';if(target.port==='80')target.port='';location.replace(target.href);return;}
   if(!state.cookie_ok)throw Error('浏览器没有传回本站 Cookie。请通过 HTTPS 打开网站并允许本站 Cookie，再重新登录。');
   if(String(state.uid??'')!==f.dataset.uid)throw Error('登录账号已改变或会话已失效，请刷新页面后重试。');
   f.elements.csrf.value=state.csrf;
   message.textContent='正在提交，请稍候…';
   HTMLFormElement.prototype.submit.call(f);
  }catch(error){message.textContent=error.message||'请求失败，请检查网络后重试。';button.disabled=false;button.textContent=label;busy=false;}
 });
 window.addEventListener('pageshow',()=>{busy=false;buttonReset();});
 function buttonReset(){const b=f.querySelector('button');if(b){b.disabled=false;if(b.textContent==='正在提交…')b.textContent=f.dataset.uid?'创建班级和班主任账号':'登录练习室 →';}}
});
