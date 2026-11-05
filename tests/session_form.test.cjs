const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function setup(state,{uid='',protocol='https:',fail=false}={}){
 let submit,submitted=0,redirected=null,requests=0;
 const button={textContent:'提交',disabled:false},message={textContent:''};
 const form={dataset:{uid},elements:{csrf:{value:'old'}},querySelector:s=>s==='[data-session-message]'?message:button,addEventListener:(name,fn)=>{if(name==='submit')submit=fn}};
 const sandbox={document:{querySelectorAll:s=>s==='[data-session-form]'?[form]:[],getElementById:()=>null,querySelector:()=>null},window:{addEventListener:()=>{}},location:{protocol,href:protocol+'//practice.test/login',replace:url=>{redirected=url}},URL,AbortSignal,HTMLFormElement:{prototype:{submit(){submitted++}}},fetch:async()=>{requests++;if(fail)throw Error('网络异常');return {ok:true,json:async()=>state}}};
 vm.runInNewContext(fs.readFileSync('static/app.js','utf8'),sandbox);
 return {form,button,message,submit:()=>submit({preventDefault(){}}),stats:()=>({submitted,redirected,requests})};
}
test('fresh token is sent on the first click and duplicate clicks submit once',async()=>{
 const t=setup({cookie_ok:true,uid:1,csrf:'fresh'},{uid:'1'});
 await Promise.all([t.submit(),t.submit()]);
 assert.equal(t.form.elements.csrf.value,'fresh');assert.equal(t.stats().submitted,1);assert.equal(t.stats().requests,1);
});
test('missing cookie blocks credential submission with a visible message',async()=>{
 const t=setup({cookie_ok:false,uid:null,csrf:'fresh'});await t.submit();
 assert.equal(t.stats().submitted,0);assert.match(t.message.textContent,/Cookie/);assert.equal(t.button.disabled,false);
});
test('HTTP with secure cookies moves to HTTPS without sending a password',async()=>{
 const t=setup({cookie_ok:false,secure:true,uid:null},{protocol:'http:'});await t.submit();
 assert.equal(t.stats().submitted,0);assert.equal(t.stats().redirected,'https://practice.test/login');
});
test('account change blocks a pending class submission',async()=>{
 const t=setup({cookie_ok:true,uid:2,csrf:'fresh'},{uid:'1'});await t.submit();
 assert.equal(t.stats().submitted,0);assert.match(t.message.textContent,/账号已改变/);
});
test('network failure allows retry without sending duplicate requests',async()=>{
 const t=setup(null,{fail:true});await t.submit();assert.equal(t.button.disabled,false);await t.submit();
 assert.equal(t.stats().submitted,0);assert.equal(t.stats().requests,2);
});
