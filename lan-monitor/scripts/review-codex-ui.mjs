import {createServer} from 'node:http'
import {readFile,mkdir,writeFile} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {resolve,join} from 'node:path'
import {createRequire} from 'node:module'
import assert from 'node:assert/strict'
const web=fileURLToPath(new URL('../../OrgPortal/web/',import.meta.url))
const {chromium}=createRequire(join(web,'package.json'))('@playwright/test')
const view=fileURLToPath(new URL('../view/',import.meta.url))
export async function reviewCodexUi(output){
 await mkdir(output,{recursive:true})
 const commands=[],results=[];let ownerConnected=true,statusUnauthorized=false
 const threads=[{id:'design',name:'Improve the remote control workspace',cwd:'/workspace/Organization',updated_at:1792880000,activity:'running'},{id:'release',name:'Review the release checks',cwd:'/workspace/OrgPortal',updated_at:1792876000,activity:'idle'}]
 threads.push(...Array.from({length:18},(_,index)=>({id:'history-'+index,name:'Earlier workspace task '+(index+1),cwd:'/workspace/Organization',updated_at:1792800000-index*300,activity:'idle'})))
 for(const thread of threads){thread.display_title=thread.name;thread.name='Raw database name';thread.title='Initial prompt text'}
 const lanFixture={updated:'2026-10-24T20:00:00Z',devices:['worker','queued','stopped','failed','passed'].map((name,index)=>({hostname:name+'.local',address:'10.0.0.'+(index+2),reachable:true})),network:{interface:'eth0',subnet:'10.0.0.0/24',gateway:'10.0.0.1',interfaces:[{name:'eth0',rx_mbps:1.25,tx_mbps:0.5,capacity_mbps:1000}]},run:{id:'fixture-run',status:'running',hosts:['running','queued','stopped','failed','passed'].map((status,index)=>({host:['worker','queued','stopped','failed','passed'][index]+'.local',status,jobs:[{name:'Fixture tests',status}]}))}};
 const server=createServer(async(req,res)=>{
  try{
   const url=new URL(req.url,'http://localhost')
   if(url.pathname==='/api/v1/swarm/status'){res.setHeader('Content-Type','application/json');res.statusCode=statusUnauthorized?401:200;res.end(JSON.stringify(lanFixture));return}
   if(url.pathname==='/swarm-status'){res.setHeader('Content-Type','text/html');res.end(await readFile(join(view,'swarm-status.html')));return}
   if(url.pathname.startsWith('/api/')){
    res.setHeader('Content-Type','application/json')
    if(req.method==='POST'){let body='';for await(const chunk of req)body+=chunk;commands.push(JSON.parse(body));res.end(JSON.stringify({accepted:true,message:'Instruction accepted by the synthetic thread.'}));return}
    const item=threads.find(t=>url.pathname.endsWith('/'+t.id))
    res.end(JSON.stringify(item?{...item,model:'Codex',owner_connected:ownerConnected,messages:[{role:'user',text:'Make the remote workspace easier to scan and use on a phone.'},{role:'assistant',text:'I’ll organize the conversation list, clarify connection status and keep the instruction composer stable while updates arrive.\n\nThe active conversation will stay visible, and your draft will be preserved.'}]}:{threads,next_offset:null,machine:'Fixture workstation',connected:true}));return
   }
   if(url.pathname==='/'){res.setHeader('Content-Type','text/html');res.end('<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Codex UI review</title><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/codex.css"><body><main><section id="codex-context"></section></main><script type="module" src="/codex.js"></script></body></html>');return}
   if(!['/style.css','/codex.css','/codex.js','/swarm-status.css','/swarm-status.js','/swarm-machines.js'].includes(url.pathname)){res.writeHead(404).end();return}
   res.setHeader('Content-Type',url.pathname.endsWith('.js')?'text/javascript':'text/css');res.end(await readFile(join(view,url.pathname.slice(1))))
  }catch(error){res.writeHead(500).end(error.message)}
 })
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const base=`http://127.0.0.1:${server.address().port}`
 const browser=await chromium.launch({headless:true,args:['--disable-gpu'],...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})})
 try{for(const width of [390,1440]){
  const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'}),page=await context.newPage(),errors=[]
  page.on('pageerror',e=>errors.push(e.message))
  await page.goto(base);await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
  const mapLink=page.getByRole('link',{name:'Swarm status ↗',exact:true});assert.equal(await mapLink.getAttribute('href'),'/swarm-status');assert.equal(await mapLink.getAttribute('target'),'_blank');assert.equal(await mapLink.isVisible(),true)
  await page.screenshot({path:join(output,`codex-locked-${width}.png`),fullPage:false})
  await page.goto(base+'/swarm-status');await page.getByRole('heading',{name:'LAN status',exact:true}).waitFor()
  await page.waitForFunction(()=>document.querySelectorAll('.machine-card').length===5)
  for(const state of ['running','queued','stopped','failed','passed'])assert.equal(await page.locator('.machine-card[data-state='+state+']').count(),1)
  assert(await page.locator('#devices').textContent().then(text=>text.includes('worker.local')))
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
  await page.screenshot({path:join(output,`codex-lan-${width}.png`),fullPage:false})
  await page.locator('.machine-card').first().focus();await page.keyboard.press('Enter');await page.getByRole('heading',{name:'worker.local',exact:true,level:2}).waitFor()
  assert.equal(await page.locator('.machine-task[data-state=running]').count(),1)
  await page.screenshot({path:join(output,`codex-machine-${width}.png`),fullPage:false})
  await page.getByRole('button',{name:'Refresh',exact:true}).click();assert(page.url().includes('#machine='))
  await page.getByRole('link',{name:'← All machines',exact:true}).click();await page.locator('.machine-card').first().waitFor();assert.equal(await page.locator('.machine-card:visible').count(),5)

  statusUnauthorized=true;await page.getByRole('button',{name:'Refresh',exact:true}).click()
  await page.waitForFunction(()=>document.getElementById('feedback')?.textContent.includes('Pair this browser'))
  assert.equal(await page.locator('#devices').textContent(),'');statusUnauthorized=false;await page.goto(base)

  await page.locator('.codex-unlock input').fill('synthetic-only');await page.getByRole('button',{name:'Connect',exact:true}).click()
  await page.locator('.codex-thread').first().waitFor()
  assert(await page.locator('.codex-thread').evaluateAll(nodes=>nodes.every(node=>[...node.children].every(child=>child.getBoundingClientRect().bottom<=node.getBoundingClientRect().bottom))), 'Conversation rows must contain their text without overlap')
  const workspace=await page.locator('.codex-workspace').boundingBox(),pagination=await page.locator('.codex-pagination').boundingBox();assert(pagination.y+pagination.height<=workspace.y+workspace.height+1,'Pagination must remain inside the workspace with twenty threads')
  await page.screenshot({path:join(output,`codex-list-${width}.png`),fullPage:false})
  await page.getByRole('searchbox').fill('missing thread');assert.equal(await page.locator('.codex-no-results').isVisible(),true)
  await page.getByRole('searchbox').fill('release');assert.equal(await page.locator('.codex-thread:visible').count(),1)
  await page.getByRole('searchbox').fill('');await page.locator('.codex-thread').first().click()
  await page.locator('.codex-message').first().waitFor()
  assert.equal(await page.locator('.codex-thread-info h2').textContent(),'Improve the remote control workspace')
  assert.equal(await page.locator('.codex-thread').first().locator('strong').textContent(),'Improve the remote control workspace')
  await page.locator('.codex-compose textarea').fill('Keep this draft while refreshing.')
  await page.getByLabel('Instruction delivery').selectOption('send')
  await page.locator('.codex-message').first().evaluate(node=>node.dataset.retained='yes')
  if(width<800){await page.getByRole('button',{name:'← Conversations',exact:true}).click()}
  await Promise.all([page.waitForResponse(r=>r.url().endsWith('/threads/design')),page.getByRole('button',{name:'Refresh',exact:true}).click()]);await page.waitForTimeout(100)
  await page.waitForFunction(()=>document.querySelector('.codex-message')?.dataset.retained==='yes')
  assert.equal(await page.getByLabel('Instruction delivery').inputValue(),'send')
  assert.equal(await page.locator('textarea').inputValue(),'Keep this draft while refreshing.')
  if(width<800)await page.locator('.codex-thread').first().click()
  assert.match(await page.locator('.codex-message>div').last().evaluate(node=>getComputedStyle(node).fontFamily),/system-ui/)
  await page.screenshot({path:join(output,`codex-conversation-${width}.png`),fullPage:false})
  const before=commands.length;await page.getByRole('button',{name:'Send instruction',exact:true}).click()
  await page.waitForFunction(()=>document.querySelector('textarea')?.value==='')
  assert.equal(commands.length,before+1);assert.equal(commands.at(-1).thread_id,'design');assert.equal(commands.at(-1).action,'send')
  ownerConnected=false;
  await page.waitForFunction(()=>document.querySelector('.codex-thread-info')?.dataset.connected==='false',{},{timeout:8000})
  assert.equal(await page.getByRole('button',{name:'Send instruction',exact:true}).isDisabled(),true);ownerConnected=true
  await page.getByRole('button',{name:'Lock',exact:true}).click();assert.equal(await page.locator('.codex-workspace').isVisible(),false)
  assert.equal(await page.locator('textarea').inputValue(),'')
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
  assert.deepEqual(errors,[]);results.push({width,status:'passed',checks:['connect','empty-state','search','select-thread','stable-refresh','draft-retained','delivery-retained','send-once','lock','no-overflow','no-script-errors','empty-search','disconnected-thread','long-list-pagination']});await context.close()
 }}finally{await browser.close();server.closeAllConnections();await new Promise(r=>server.close(r));await writeFile(join(output,'codex-review.json'),JSON.stringify({mode:'synthetic-local-only',results},null,2)+'\n')}
 return results
}
if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url))await reviewCodexUi(resolve(process.argv[2]||'artifacts/codex-ui'))
