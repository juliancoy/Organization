import {DurableObject} from 'cloudflare:workers';
import {verifyTicket, type Ticket} from './auth';

type Attachment = Ticket & {window: number; count: number};
const json = (value: unknown, status = 200) => Response.json(value, {status, headers:{'Cache-Control':'no-store'}});

function socketTicket(request: Request) {
  const protocols = (request.headers.get('Sec-WebSocket-Protocol') || '').split(',').map(x=>x.trim());
  return protocols.includes('archive.v1') ? protocols.find(x=>x.startsWith('auth.'))?.slice(5) : undefined;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === '/health') return json({service:'organization-archive-peers', version:env.CF_VERSION_METADATA.id,
      signaling:'websocket', transport:'webrtc', turnConfigured:!!(env.TURN_KEY_ID && env.TURN_KEY_API_TOKEN)});
    if (url.pathname === '/signal' || url.pathname === '/ice') {
      const origin = request.headers.get('Origin');
      const allowed = [url.origin, ...env.PEER_ALLOWED_ORIGINS.split(',')];
      if (origin && !allowed.includes(origin)) return json({error:'Origin denied'},403);
      const cors:Record<string,string>=origin?{'Access-Control-Allow-Origin':origin,'Vary':'Origin'}:{};
      if (url.pathname==='/ice' && request.method==='OPTIONS') return new Response(null,{status:204,headers:{...cors,
        'Access-Control-Allow-Methods':'GET','Access-Control-Allow-Headers':'Authorization','Access-Control-Max-Age':'600'}});
      let ticket: Ticket;
      try {
        const token = url.pathname === '/signal' ? socketTicket(request) : request.headers.get('Authorization')?.replace(/^Bearer /,'');
        ticket = await verifyTicket(token || '', env.PEER_SIGNING_KEY);
      } catch {return json({error:'Invalid or expired device invitation'},401);}
      if (url.pathname === '/ice') {
        if (request.method !== 'GET') return json({error:'Method denied'},405);
        // Route credential issuance through the room to cache one bounded lease per device.
        const response=await env.SIGNAL_ROOMS.getByName(ticket.room).ice(ticket);
        const headers=new Headers(response.headers);for (const [key,value] of Object.entries(cors)) if(value) headers.set(key,value);
        return new Response(response.body,{status:response.status,headers});
      }
      if (request.headers.get('Upgrade')?.toLowerCase() !== 'websocket') return json({error:'WebSocket required'},426);
      // The DO verifies the ticket again; no identity-bearing internal headers are trusted.
      return env.SIGNAL_ROOMS.getByName(ticket.room).fetch(request);
    }
    const response = await env.ASSETS.fetch(request);
    const headers = new Headers(response.headers);
    headers.set('Referrer-Policy','no-referrer');
    headers.set('X-Content-Type-Options','nosniff');
    headers.set('Content-Security-Policy',"default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'");
    return new Response(response.body,{status:response.status,headers});
  }
} satisfies ExportedHandler<Env>;

export class SignalRoom extends DurableObject<Env> {
  async fetch(request: Request): Promise<Response> {
    let ticket: Ticket;
    try {ticket=await verifyTicket(socketTicket(request)||'',this.env.PEER_SIGNING_KEY);}
    catch {return json({error:'Invalid invitation'},401);}
    await this.expire();
    const sockets=this.ctx.getWebSockets();
    if (sockets.length >= 24) return json({error:'Room full'},429);
    if (sockets.some(ws=>(ws.deserializeAttachment() as Attachment).peer===ticket.peer)) return json({error:'Device already connected'},409);
    const pair=new WebSocketPair();const [client,server]=Object.values(pair);
    server.serializeAttachment({...ticket,window:Date.now(),count:0} satisfies Attachment);
    this.ctx.acceptWebSocket(server,[ticket.peer]);
    const alarm=await this.ctx.storage.getAlarm();
    if (!alarm || alarm>ticket.exp*1000) await this.ctx.storage.setAlarm(ticket.exp*1000);
    server.send(JSON.stringify({type:'welcome',peer:ticket.peer,room:ticket.room,peers:sockets.map(ws=>(ws.deserializeAttachment() as Attachment).peer)}));
    for (const ws of sockets) ws.send(JSON.stringify({type:'joined',peer:ticket.peer}));
    return new Response(null,{status:101,webSocket:client,headers:{'Sec-WebSocket-Protocol':'archive.v1'}});
  }

  async ice(ticket: Ticket): Promise<Response> {
    const fallback={iceServers:[{urls:['stun:stun.cloudflare.com:3478']}],relayAvailable:false};
    if (!this.env.TURN_KEY_ID || !this.env.TURN_KEY_API_TOKEN) return json(fallback);
    const cached=await this.ctx.storage.get<{expires:number;value:unknown}>('ice:'+ticket.peer);
    if (cached && cached.expires>Date.now()+60000) return json(cached.value);
    const ttl=Math.min(3600,ticket.exp-Math.floor(Date.now()/1000));
    if (ttl<=0) return json({error:'Invitation expired'},401);
    const response=await fetch(`https://rtc.live.cloudflare.com/v1/turn/keys/${this.env.TURN_KEY_ID}/credentials/generate-ice-servers`,{
      method:'POST',headers:{Authorization:'Bearer '+this.env.TURN_KEY_API_TOKEN,'Content-Type':'application/json'},body:JSON.stringify({ttl}),signal:AbortSignal.timeout(10000)});
    if (!response.ok) return json({error:'TURN credential service unavailable'},503);
    const data=await response.json() as {iceServers:Array<{urls:string[];username?:string;credential?:string}>};
    const value={iceServers:data.iceServers,relayAvailable:true};
    await this.ctx.storage.put('ice:'+ticket.peer,{expires:Date.now()+ttl*1000,value});
    const alarm=await this.ctx.storage.getAlarm();
    if (!alarm || alarm>ticket.exp*1000) await this.ctx.storage.setAlarm(ticket.exp*1000);
    return json(value);
  }

  webSocketMessage(ws: WebSocket, message: string | ArrayBuffer) {
    const a=ws.deserializeAttachment() as Attachment;
    if (a.exp*1000<=Date.now()) {ws.close(1008,'Invitation expired');return;}
    if (typeof message!=='string' || new TextEncoder().encode(message).length>65536) {ws.close(1009,'Signal too large');return;}
    if (Date.now()-a.window>10000) {a.window=Date.now();a.count=0;}
    if (++a.count>200) {ws.close(1008,'Signal rate exceeded');return;}
    ws.serializeAttachment(a);
    try {
      const data=JSON.parse(message) as {type:string;to:string;description?:{type:string;sdp:string};candidate?:unknown};
      if (!['offer','answer','candidate'].includes(data.type) || typeof data.to!=='string') throw new Error();
      if (data.type==='candidate' && (data.candidate===null || typeof data.candidate!=='object')) throw new Error();
      if (data.type!=='candidate' && (!data.description || data.description.type!==data.type || typeof data.description.sdp!=='string')) throw new Error();
      const target=this.ctx.getWebSockets(data.to)[0];
      if (target && (target.deserializeAttachment() as Attachment).exp*1000>Date.now()) {
        target.send(JSON.stringify({type:data.type,from:a.peer,...(data.type==='candidate'?{candidate:data.candidate}:{description:data.description})}));
      }
    } catch {ws.close(1008,'Invalid signal');}
  }

  webSocketClose(ws: WebSocket, code: number) {
    const a=ws.deserializeAttachment() as Attachment;
    ws.close(code===1005?1000:code);
    for (const other of this.ctx.getWebSockets()) if (other!==ws) {
      try {other.send(JSON.stringify({type:'left',peer:a.peer}));} catch { /* Already closed. */ }
    }
  }
  webSocketError(ws: WebSocket) {ws.close(1011,'Connection error');}
  private async expire() {
    let next=Infinity;
    for (const ws of this.ctx.getWebSockets()) {
      const a=ws.deserializeAttachment() as Attachment;
      if (a.exp*1000<=Date.now()) ws.close(1008,'Invitation expired');else next=Math.min(next,a.exp*1000);
    }
    const leases=await this.ctx.storage.list<{expires:number}>({prefix:'ice:'});
    for (const [key,lease] of leases) {
      if (lease.expires<=Date.now()) await this.ctx.storage.delete(key);else next=Math.min(next,lease.expires);
    }
    if (Number.isFinite(next)) await this.ctx.storage.setAlarm(next);else await this.ctx.storage.deleteAlarm();
  }
  async alarm() {await this.expire();}
}
