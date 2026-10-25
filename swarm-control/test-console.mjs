// Isolated local console session, real browser-to-aiortc WebRTC, real test APK.
import {spawn,execFileSync} from 'node:child_process'
import {mkdtemp,mkdir,readFile,writeFile,rm} from 'node:fs/promises'
import {tmpdir} from 'node:os'
import {join,resolve} from 'node:path'
import {fileURLToPath} from 'node:url'
import {randomBytes,createHash} from 'node:crypto'
import assert from 'node:assert/strict'
import {chromium} from '@playwright/test'
const workspace=fileURLToPath(new URL('../',import.meta.url))
const output=join(workspace,'artifacts/swarm-control-deployment');await mkdir(output,{recursive:true})
const temp=await mkdtemp(join(tmpdir(),'swarm-control-fixture-'))
execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=localhost','-keyout',join(temp,'key.pem'),'-out',join(temp,'cert.pem')],{stdio:'ignore'})
const source=`import sys,json,os,ssl\nsys.path.insert(0,${JSON.stringify(join(workspace,'swarm-control/server'))})\nfrom http.server import ThreadingHTTPServer\nfrom api import Handler\nimport devices\ns=ThreadingHTTPServer(('127.0.0.1',0),Handler)\ncontext=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)\ncontext.load_cert_chain(os.environ['SWARM_TEST_CERT'],os.environ['SWARM_TEST_KEY'])\ns.socket=context.wrap_socket(s.socket,server_side=True)\ns.public_origin='https://localhost:'+str(s.server_address[1])\nprint(json.dumps({'origin':s.public_origin,'session':devices.session(devices.issue())}),flush=True)\ns.serve_forever()`
const python=process.env.SWARM_P2P_PYTHON||join(process.env.HOME,'.local/share/swarm-control/venv/bin/python')
const server=spawn(python,['-u','-c',source],{env:{...process.env,MAP_CODEX_DEVICE_FILE:join(temp,'devices'),SWARM_TEST_CERT:join(temp,'cert.pem'),SWARM_TEST_KEY:join(temp,'key.pem')},stdio:['ignore','pipe','pipe']})
let browser,errors=''
server?.stderr.on('data',v=>{errors+=v})
try{
 const fixture=await new Promise((resolve,reject)=>{let pending='';const timeout=setTimeout(()=>reject(Error('Fixture server startup timeout')),15000);server.stdout.on('data',v=>{pending+=v;if(pending.includes('\n')){clearTimeout(timeout);resolve(JSON.parse(pending.split('\n')[0]))}});server.once('exit',code=>{clearTimeout(timeout);reject(Error('Fixture server exited '+code+': '+errors))})})
 const {origin,session}=fixture
 browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,args:['--disable-gpu']})
 const context=await browser.newContext({viewport:{width:390,height:844},acceptDownloads:true,ignoreHTTPSErrors:true})
 await context.addCookies([{name:'__Host-compai_device',value:session,url:origin,secure:true,httpOnly:true,sameSite:'Strict'}])
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
 const report={passed:true,physicalDevice:false,mode:'isolated Chrome to Swarm Control WebRTC fixture',bytes:received.length,sha256:createHash('sha256').update(received).digest('hex'),checks:['paired authorization','WebRTC data channel','APK byte equality','browser SHA-256 validation','receipt','narrow layout','disconnect']}
 await writeFile(join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report))
}finally{if(browser)await browser.close();if(server){server.kill('SIGTERM');await new Promise(resolve=>server.once('close',resolve));}await rm(temp,{recursive:true,force:true})}
