// Isolated local console session, real browser-to-aiortc WebRTC, real test APK.
import {spawn} from 'node:child_process'
import {mkdtemp,mkdir,readFile,writeFile,rm} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join,resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomBytes,createHash} from 'node:crypto'
import assert from 'node:assert/strict'
import {chromium} from '@playwright/test'
const workspace=fileURLToPath(new URL('../',import.meta.url))
const output=join(workspace,'artifacts/swarm-control-deployment');await mkdir(output,{recursive:true})
const live=process.env.SWARM_LIVE_CONSOLE_TEST==='1'
const temp=await mkdtemp(join(tmpdir(),'lifetech-phone-fixture-')),token=live?(await readFile(join(process.env.HOME,'.config/map-master/codex-token'),'utf8')).trim():randomBytes(32).toString('hex')
if(!live)await writeFile(join(temp,'owner'),token,{mode:0o600})
const source=`import sys,json,os\nsys.path.insert(0,${JSON.stringify(join(workspace,'lan-monitor'))})\nfrom http.server import ThreadingHTTPServer\nfrom codex_remote import Handler\ns=ThreadingHTTPServer(('127.0.0.1',0),Handler)\ns.public_origin='http://127.0.0.1:'+str(s.server_address[1])\nprint(json.dumps({'origin':s.public_origin}),flush=True)\ns.serve_forever()`
const python=process.env.SWARM_P2P_PYTHON||join(process.env.HOME,'.local/share/lifetech-p2p-venv/bin/python')
const server=live?null:spawn(python,['-u','-c',source],{env:{...process.env,MAP_CODEX_TOKEN_FILE:join(temp,'owner'),MAP_CODEX_DEVICE_FILE:join(temp,'devices'),PYTHONPATH:join(process.env.HOME,'.local/share/compai-codex/python')},stdio:['ignore','pipe','pipe']})
let browser,errors=''
server?.stderr.on('data',v=>{errors+=v})
try{
 const origin=live?'https://compai.lifetech.fyi':await new Promise((resolve,reject)=>{let pending='';const timeout=setTimeout(()=>reject(Error('Fixture server startup timeout')),15000);server.stdout.on('data',v=>{pending+=v;if(pending.includes('\n')){clearTimeout(timeout);resolve(JSON.parse(pending.split('\n')[0]).origin)}});server.once('exit',code=>{clearTimeout(timeout);reject(Error('Fixture server exited '+code+': '+errors))})})
 browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,args:['--disable-gpu']})
 const context=await browser.newContext({viewport:{width:390,height:844},acceptDownloads:true})
 await context.route(origin+'/api/v1/test-app/**',route=>route.continue({headers:{...route.request().headers(),Authorization:'Bearer '+token}}))
 const page=await context.newPage();await page.goto(origin+'/swarm-control')
 await page.getByRole('button',{name:'Connect P2P',exact:true}).click()
 await page.getByRole('status').filter({hasText:'Direct WebRTC connected'}).waitFor({timeout:60000})
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
 await page.screenshot({path:join(output,'phone-p2p-connected.png')})
 const transfer=page.waitForEvent('download',{timeout:30000})
 await page.getByRole('button',{name:'Receive test APK',exact:true}).click()
 const download=await transfer;await download.saveAs(join(output,'received-test.apk'))
 await page.getByRole('status').filter({hasText:'SHA-256 verified'}).waitFor()
 const original=await readFile(join(workspace,'.runtime/mobile/swarm-control-test.apk')),received=await readFile(join(output,'received-test.apk'))
 assert.deepEqual(received,original)
 const status=await page.evaluate(async()=>await(await fetch('/api/v1/test-app/status')).json())
 assert.ok(status.peers.some(p=>p.apkReceived&&p.state==='connected'))
 await page.screenshot({path:join(output,'phone-p2p-received.png')})
 await page.getByRole('button',{name:'Disconnect',exact:true}).click()
 await page.getByRole('status').filter({hasText:'Disconnected from CompAI'}).waitFor()
 const report={passed:true,physicalDevice:false,mode:live?'Chrome to live compai.lifetech.fyi WebRTC deployment':'isolated Chrome to CompAI Python WebRTC fixture',bytes:received.length,sha256:createHash('sha256').update(received).digest('hex'),checks:['paired authorization','WebRTC data channel','APK byte equality','browser SHA-256 validation','receipt','narrow layout','disconnect']}
 await writeFile(join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report))
}finally{if(browser)await browser.close();if(server){server.kill('SIGTERM');await new Promise(resolve=>server.once('close',resolve));}await rm(temp,{recursive:true,force:true})}
