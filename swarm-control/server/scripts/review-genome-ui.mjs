import {createServer} from 'node:http';
import {readFile,mkdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {join} from 'node:path';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const require=createRequire(new URL('../../package.json',import.meta.url));
const {chromium}=require('@playwright/test');
const view=fileURLToPath(new URL('../view/',import.meta.url));
const output=process.argv[2]||'/tmp/genome-interactions';await mkdir(output,{recursive:true});
const names=new Set(['genome-explorer.html','genome-explorer.js','genome-explorer.css','genome-data.js','swarm-control.svg']);
const server=createServer(async(req,res)=>{const name=new URL(req.url,'http://localhost').pathname.slice(1);if(!names.has(name)){res.writeHead(404).end();return;}try{const content=await readFile(join(view,name));res.setHeader('Content-Type',name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':name.endsWith('.svg')?'image/svg+xml':'text/html');res.end(content);}catch{res.writeHead(500).end();}});
await new Promise(r=>server.listen(0,'127.0.0.1',r));
const browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true});
try{
 for(const width of [390,1440]){
  const context=await browser.newContext({viewport:{width,height:1000}});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`http://127.0.0.1:${server.address().port}/genome-explorer.html`);
  await page.locator('#metrics strong').first().waitFor();
  for(const name of ['Julian','HG001','HG002','HG005','Watson','Venter']){
   await page.selectOption('#sample',name);
   assert.match(await page.locator('#sample-note').innerText(),new RegExp(name));
   assert.equal(await page.locator('#metrics .metric').count(),4);
   await page.locator('#chromosomes button[data-chrom="21"]').click();
   assert.match(await page.locator('#line-title').innerText(),/Chromosome 21/);
   assert.equal(await page.locator('#line svg circle').count()>0,true);
   if(['Watson','Venter'].includes(name)){assert.match(await page.locator('#satellite-stat').innerText(),/unavailable/);assert.match(await page.locator('#depth').innerText(),/not supplied/);}
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`${name} overflow at ${width}`);
  }
  await page.selectOption('#sample','Julian');await page.locator('#chromosomes button[data-chrom="MT"]').click();
  assert.equal(await page.locator('#line svg circle').count(),1,'Single mitochondrial bin must not invent a second zero bin');
  await page.locator('#methods summary').click();assert.equal(await page.locator('#methods').getAttribute('open'),'');
  await page.locator('#present').click();assert.equal(await page.locator('body').getAttribute('class'),'presentation');
  if(await page.evaluate(()=>!!document.fullscreenElement))await page.evaluate(()=>document.exitFullscreen());
  await page.evaluate(()=>document.body.classList.remove('presentation'));
  await page.locator('#chromosomes button[data-chrom="1"]').click();
  await page.screenshot({path:join(output,`genome-${width}.png`),fullPage:true});
  assert.deepEqual(errors,[]);await context.close();
 }
 console.log('Passed: six genomes, chromosome navigation, absent metadata, single-bin plots, methods, presentation, desktop/mobile overflow and browser errors.');
}finally{await browser.close();await new Promise(r=>server.close(r));}
