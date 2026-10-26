import assert from 'node:assert/strict'
import {createServer} from 'node:http'
import {readFile,mkdir} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {join} from 'node:path'
import {chromium,expect} from '@playwright/test'
const view=fileURLToPath(new URL('../view/',import.meta.url)),output=process.argv[2]||'/tmp/resource-network-review';await mkdir(output,{recursive:true})
const network=time=>({devices:[{hostname:'worker.local',ip:'10.42.0.2',mac:'aa'},{ip:'10.42.0.3',mac:'bb'}],link_traffic:{updated:new Date(time).toISOString(),peers:{aa:{rx_mbps:10,tx_mbps:4}}}})
const server=createServer(async(req,res)=>{
 const url=new URL(req.url,'http://localhost');res.setHeader('Cache-Control','no-store')
 if(req.method!=='GET'){res.writeHead(405).end();return}
 if(url.pathname==='/api/v1/swarm/task-manager'){
  const now=Date.now(),host={host:'worker.local',addresses:['10.42.0.2'],sampled_at:now/1000,status:'live',percent:20,cpus:4,network_interfaces:[{name:'eth0',ifindex:2,state:'up',status:'live',sampled_at:now/1000,rx_mbps:4,tx_mbps:10,network_mbps:14},{name:'wlan0',ifindex:3,state:'up',status:'live',sampled_at:now/1000,rx_mbps:2,tx_mbps:1,network_mbps:3}]};
  const history=Array.from({length:13},(_,n)=>{const time=now-60000+n*5000;return {sampled_at:time/1000,hosts:[{...host,sampled_at:time/1000,network_interfaces:host.network_interfaces.map(iface=>({...iface,sampled_at:time/1000}))}],network:network(time)}});
  res.setHeader('Content-Type','application/json');res.end(JSON.stringify({network:network(now),metrics:{hosts:[host],history},run:{hosts:[]}}));return
 }
 if(url.pathname==='/api/v1/swarm/connections'){res.setHeader('Content-Type','application/json');res.end('{"hosts":[]}');return}
 const name=url.pathname==='/task-manager'?'task-manager.html':url.pathname.slice(1)
 if(!/^[a-z-]+\.(?:js|css|html|svg)$/.test(name)){res.writeHead(404).end();return}
 try{res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html');res.end(await readFile(join(view,name)))}catch{res.writeHead(404).end()}
})
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})})
try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',error=>errors.push(error.message))
 await page.goto(`http://127.0.0.1:${server.address().port}/task-manager`)
 await expect(page.locator('#levels-status')).toContainText('Updated')
 await page.getByRole('button',{name:'Network',exact:true}).click()
 await page.getByLabel('Separate send / receive').check()
 const paths=await page.locator('#levels-plot path[fill="none"]').evaluateAll(paths=>paths.map(path=>path.getAttribute('d')))
 assert.equal(paths.length,4,'Two interfaces should each have continuous send and receive series')
 await expect(page.locator('.levels-machine-group').first()).toHaveText('worker')
 await expect(page.locator('#levels-legend button').first()).toHaveAttribute('aria-label',/worker.local · eth0/)
 await expect(page.locator('.levels-machine-group').last()).toHaveText('Gateway-only devices')
 await expect(page.locator('#levels-legend')).not.toContainText('interface unknown')
 await expect(page.getByRole('button',{name:/worker.local · eth0/})).toHaveCount(2)
 await expect(page.getByRole('button',{name:/worker.local · wlan0/})).toHaveCount(2)
 assert(paths.every(path=>(path.match(/L/g)||[]).length>=12),'Restored network history should contain all background samples')
 const send=page.getByRole('button',{name:/worker.local · eth0 · send/});
 await send.click();await expect(page.getByRole('button',{name:/worker.local · eth0 · send/})).toHaveAttribute('aria-pressed','false');
 await page.getByRole('button',{name:/worker.local · eth0 · send/}).click();
 const boxes=await page.locator('.levels-interface').evaluateAll(cards=>cards.map(card=>{const r=card.getBoundingClientRect();return {top:r.top,bottom:r.bottom}}));
 assert(boxes.every((box,index)=>index===0||box.top>=boxes[index-1].bottom),'Interface cards overlap');
 await page.locator('#device-levels').screenshot({path:join(output,'network-history.png')})
 await page.setViewportSize({width:390,height:1000});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
 await page.locator('#device-levels').screenshot({path:join(output,'network-mobile.png')})
 assert.deepEqual(errors,[])
 console.log('PASS: directional network history renders continuous measured samples')
}finally{await browser.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve))}
