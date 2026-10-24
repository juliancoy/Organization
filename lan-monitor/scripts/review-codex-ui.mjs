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
 const commands=[],results=[];let ownerConnected=true
 const threads=[{id:'design',name:'Improve the remote control workspace',cwd:'/workspace/Organization',updated_at:1792880000,activity:'running'},{id:'release',name:'Review the release checks',cwd:'/workspace/OrgPortal',updated_at:1792876000,activity:'idle'}]
 threads.push(...Array.from({length:18},(_,index)=>({id:'history-'+index,name:'Earlier workspace task '+(index+1),cwd:'/workspace/Organization',updated_at:1792800000-index*300,activity:'idle'})))
 const server=createServer(async(req,res)=>{
  try{
   const url=new URL(req.url,'http://localhost')
   if(url.pathname.startsWith('/api/')){
    res.setHeader('Content-Type','application/json')
    if(req.method==='POST'){let body='';for await(const chunk of req)body+=chunk;commands.push(JSON.parse(body));res.end(JSON.stringify({accepted:true,message:'Instruction accepted by the synthetic thread.'}));return}
    const item=threads.find(t=>url.pathname.endsWith('/'+t.id))
    res.end(JSON.stringify(item?{...item,model:'Codex',owner_connected:ownerConnected,messages:[{role:'user',text:'Make the remote workspace easier to scan and use on a phone.'},{role:'assistant',text:'I’ll organize the conversation list, clarify connection status and keep the instruction composer stable while updates arrive.\n\nThe active conversation will stay visible, and your draft will be preserved.'}]}:{threads,next_offset:null,machine:'Fixture workstation',connected:true}));return
   }
   if(url.pathname==='/'){res.setHeader('Content-Type','text/html');res.end('<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Codex UI review</title><link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/codex.css"><body><main><section id="codex-context"></section></main><script type="module" src="/codex.js"></script></body></html>');return}
   if(!['/style.css','/codex.css','/codex.js'].includes(url.pathname)){res.writeHead(404).end();return}
   res.setHeader('Content-Type',url.pathname.endsWith('.js')?'text/javascript':'text/css');res.end(await readFile(join(view,url.pathname.slice(1))))
  }catch(error){res.writeHead(500).end(error.message)}
 })
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const base=`http://127.0.0.1:${server.address().port}`
 const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})})
 try{for(const width of [390,1440]){
  const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'}),page=await context.newPage(),errors=[]
  page.on('pageerror',e=>errors.push(e.message))
  await page.goto(base);await page.screenshot({path:join(output,`codex-locked-${width}.png`),fullPage:false})
  await page.locator('.codex-unlock input').fill('synthetic-only');await page.getByRole('button',{name:'Connect',exact:true}).click()
  await page.locator('.codex-thread').first().waitFor()
  const workspace=await page.locator('.codex-workspace').boundingBox(),pagination=await page.locator('.codex-pagination').boundingBox();assert(pagination.y+pagination.height<=workspace.y+workspace.height+1,'Pagination must remain inside the workspace with twenty threads')
  await page.screenshot({path:join(output,`codex-list-${width}.png`),fullPage:false})
  await page.getByRole('searchbox').fill('missing thread');assert.equal(await page.locator('.codex-no-results').isVisible(),true)
  await page.getByRole('searchbox').fill('release');assert.equal(await page.locator('.codex-thread:visible').count(),1)
  await page.getByRole('searchbox').fill('');await page.locator('.codex-thread').first().click()
  await page.locator('.codex-message').first().waitFor()
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
