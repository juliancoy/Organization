// Synthetic local controller sessions; actual PIdP protocol tests run separately.
import {spawn,execFileSync} from 'node:child_process';
import {mkdtemp,mkdir,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';import {join,resolve} from 'node:path';import assert from 'node:assert/strict';
import {chromium} from '@playwright/test';
const root=resolve(import.meta.dirname,'server'),temp=await mkdtemp(join(tmpdir(),'swarm-pidp-')),out=resolve(import.meta.dirname,'../artifacts/swarm-pidp');await mkdir(out,{recursive:true});
execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=localhost','-keyout',join(temp,'key'),'-out',join(temp,'cert')],{stdio:'ignore'});
const source=`import sys,os,json,ssl\nsys.path.insert(0,${JSON.stringify(root)})\nfrom api import Handler\nimport identity,devices,codex,message_queue\nfrom types import SimpleNamespace\nmessage_queue.manager=lambda:SimpleNamespace(view=lambda thread:{'messages':[]})\nfrom http.server import ThreadingHTTPServer\nfrom urllib.parse import urlsplit,parse_qs\nclass Fixture(Handler):\n def do_GET(self):\n  path=urlsplit(self.path).path\n  if path=='/fixture-authorize':return self.reply(303,{},location='/auth/callback?state=fixture&code=fixture&iss=https%3A%2F%2Fidentity.example')\n  if path=='/fixture-avatar.svg':return self.reply(200,b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="#6c9"/></svg>','image/svg+xml')\n  return super().do_GET()\ndef login(origin,current=None,label='Browser'):\n return origin+'/fixture-authorize','__Host-compai_login=fixture; Path=/; Secure; HttpOnly; SameSite=Lax'\ndef complete(*args):\n with devices.state() as value:return devices.new_session(value,'Test browser',pidp_identity='synthetic-owner',pidp_subject='synthetic-owner')\ndef profile(origin,token):\n row=devices.describe(token);return {k:row[k] for k in ('label','created','last_seen','expires')}|{'auth_method':'pidp','user':{'name':'Synthetic Owner','avatar_url':origin+'/fixture-avatar.svg'}}\nidentity.start=login;identity.complete=complete;identity.session_info=profile;identity.config=lambda origin:{'owner_canonical_user_id':'synthetic-owner','allowed_subjects':['synthetic-owner']}\ncodex.rest=lambda method,path,*args:(200,{'threads':[{'id':'11111111-1111-4111-8111-111111111111','display_title':'Navigation fixture','cwd':'/fixture','activity':'idle','updated_at':1}],'next_offset':None,'machine':'Fixture','connected':True} if path.endswith('/threads') else {'display_title':'Navigation fixture','cwd':'/fixture','model':'Fixture','owner_connected':True,'activity':'idle','messages':[{'role':'assistant','text':'**Navigation** test response'}]})\nserver=ThreadingHTTPServer(('127.0.0.1',0),Fixture);ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(${JSON.stringify(join(temp,'cert'))},${JSON.stringify(join(temp,'key'))});server.socket=ctx.wrap_socket(server.socket,server_side=True);server.public_origin='https://localhost:'+str(server.server_address[1]);print(server.public_origin,flush=True);server.serve_forever()`;
const proc=spawn('python3',['-u','-c',source],{env:{...process.env,MAP_CODEX_DEVICE_FILE:join(temp,'devices.json'),SWARM_STATE_DIR:join(temp,'state')},stdio:['ignore','pipe','pipe']});let browser;let stderr='';proc.stderr.on('data',d=>stderr+=d);
try{
 const origin=await new Promise((resolve,reject)=>{let line='';proc.stdout.on('data',data=>{line+=data;if(line.includes('\n'))resolve(line.trim())});proc.once('exit',()=>reject(Error(stderr)));});
 browser=await chromium.launch({headless:true,executablePath:'/usr/bin/google-chrome'});
 const contexts=[];
 for(const width of [1440,390]){
  const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width,height:950}});contexts.push(context);const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(origin);await page.getByRole('button',{name:'Sign in with PIdP'}).click();await page.locator('.pidp-user').waitFor({timeout:5000});await page.waitForFunction(()=>document.querySelector('.pidp-user img')?.naturalWidth>0);
  assert.equal(await page.locator('.pidp-user-name').textContent(),'Synthetic Owner');assert.equal(await page.getByPlaceholder('Enter your owner token').count(),0);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.equal(await page.evaluate(async()=> (await fetch('/pair',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"token":"obsolete-pairing-token"}'})).status),404);
  await page.locator('.codex-thread').first().click();await page.locator('.codex-thread-info h2').waitFor();
  const draft=page.getByRole('textbox',{name:'Instruction to the selected thread'});await draft.fill('Keep my unsent draft');
  await page.evaluate(()=>history.back());await page.waitForFunction(()=>!document.querySelector('.codex-workspace').classList.contains('codex-reading'));
  assert.equal(new URL(page.url()).origin,origin);assert.equal(await page.locator('.codex-thread').first().isVisible(),true);
  await page.evaluate(()=>history.forward());await page.waitForFunction(()=>document.querySelector('.codex-workspace').classList.contains('codex-reading'));
  assert.equal(await draft.inputValue(),'Keep my unsent draft');
  await page.reload();await page.waitForFunction(()=>document.querySelector('.codex-workspace')?.classList.contains('codex-reading'));assert.equal(await draft.inputValue(),'Keep my unsent draft');
  if(width===390){await page.getByRole('button',{name:'← Conversations',exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.codex-workspace').classList.contains('codex-reading'));}
  console.log('Back/Forward, reload and draft preservation checks:',width);
  await page.screenshot({path:join(out,`pidp-${width}.png`)});assert.deepEqual(errors,[]);console.log('PIdP sign-in UI, avatar, retired pairing and responsive checks:',width);
 }
 const first=contexts[0].pages()[0],second=contexts[1].pages()[0];await first.getByRole('button',{name:'Lock',exact:true}).click();
 assert.equal(await second.evaluate(async()=> (await fetch('/api/v1/master/codex/threads')).status),200);
 assert.equal(await first.evaluate(async()=> (await fetch('/api/v1/master/codex/threads',{headers:{Authorization:'Bearer obsolete-owner-token'}})).status),401);
 console.log('Independent devices and retired bearer access verified');
}finally{await browser?.close();proc.kill('SIGTERM');await rm(temp,{recursive:true,force:true});}
