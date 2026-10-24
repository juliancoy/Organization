const root = document.querySelector('#codex-context');
const secureRemote=root.dataset.remote==='true' && location.protocol==='https:';
const allowedBrowser=secureRemote || ['localhost','127.0.0.1','[::1]'].includes(location.hostname);
const el = (tag, text, cls) => { const node = document.createElement(tag); if (text) node.textContent = text; if (cls) node.className = cls; return node; };
let token = '', selected = null, offset = 0, next = null, busy = false, generation = 0, renderedThread = '', renderedMessages = '', observedActivity = '';
root.classList.add('codex-app');
const heading = el('div', '', 'panel-heading');
const title = el('div'); title.append(el('span', 'SWARM · REMOTE CONTROL', 'codex-eyebrow'), el('h1', secureRemote ? 'Swarm Control' : 'Codex workspace'), el('p', secureRemote ? 'Conversations and instructions on CompAI' : 'Conversations and instructions on this machine', 'muted'));
const lock = el('button', 'Lock'); lock.hidden = true;
const mapLink=el('a','Swarm status ↗','codex-map-link'); mapLink.href='http://map.local/'; mapLink.target='_blank'; mapLink.rel='noopener noreferrer'; mapLink.title='Open the local swarm map (requires the local network)';
const headerActions=el('div','','codex-header-actions'); headerActions.append(mapLink,lock); heading.append(title,headerActions);
const unlock = el('form', '', 'codex-unlock');
const label = el('label', 'Codex owner token '), password = el('input'); password.type = 'password'; password.autocomplete = 'off'; password.required = true; password.placeholder = 'Enter your owner token'; label.append(password);
const connect = el('button', 'Connect'); connect.type = 'submit'; unlock.append(label, connect, el('small', secureRemote ? 'Open a one-time pairing link from CompAI, or use your owner token once. This browser stays paired for 30 days; Lock revokes it.' : 'Use localhost or an SSH tunnel. This separate owner token is never shared with test workers.'));
if(!allowedBrowser){password.disabled=true;connect.disabled=true;}
const status = el('p', 'Connect to view your conversations.', 'codex-status'); status.setAttribute('role', 'status');
const workspace = el('div', '', 'codex-workspace'); workspace.hidden = true;
const sidebar = el('div', '', 'codex-sidebar'), list = el('div', '', 'codex-list'), controls = el('div', '', 'codex-pagination');
const searchLabel=el('label','Find a conversation','codex-search'), search=el('input'); search.type='search'; search.placeholder='Search this page of conversations'; searchLabel.append(search);
const noResults=el('p','No conversations match. Try another name or load an older page.','codex-no-results'); noResults.hidden=true; noResults.setAttribute('role','status');
const sidebarHeading=el('h2','Conversations'); sidebar.prepend(sidebarHeading,searchLabel);
list.setAttribute('aria-label','Conversations');
const previous = el('button', 'Newer'), more = el('button', 'Older'), refresh = el('button', 'Refresh'); controls.append(previous, more, refresh); sidebar.append(list, noResults, controls);
const newChat = el('button', 'New chat'); newChat.type = 'button';
const sidebarTitle=el('div','','codex-sidebar-heading'); sidebarTitle.append(sidebarHeading,newChat); sidebar.prepend(sidebarTitle);
const conversation = el('div', '', 'codex-conversation'), info = el('div'), transcript = el('div', '', 'codex-transcript'); transcript.tabIndex = 0; transcript.setAttribute('aria-label', 'Conversation messages');
const empty=el('div','','codex-empty'); empty.append(el('span','Your workspace, at a glance','codex-eyebrow'),el('h2','Choose a conversation'),el('p','Select a thread to read its history and send the next instruction. You can review any conversation before taking action.'));
const back=el('button','← Conversations','codex-back'); back.type='button'; back.onclick=()=>{workspace.classList.remove('codex-reading');list.querySelector('[aria-pressed=true]')?.focus();};
const form = el('form', '', 'codex-compose'), promptLabel = el('label', 'Instruction to the selected thread'), input = el('textarea'); input.rows = 3; input.maxLength = 16000; input.required = true; input.placeholder = 'Describe the next task or correction…'; promptLabel.append(input);
const actions = el('div', '', 'codex-actions'), mode = el('select'); mode.setAttribute('aria-label', 'Instruction delivery');
for (const [value, text] of [['send', 'Send a new message'], ['steer', 'Steer the running turn']]) { const option = el('option', text); option.value = value; mode.append(option); }
const send = el('button', 'Send instruction'); send.type = 'submit'; send.disabled=true; const open = el('button', 'Open in VS Code'); open.type = 'button'; actions.append(mode, send, open); form.append(promptLabel, actions, el('small', 'Uses this thread’s existing permissions. Handle approval requests in local VS Code.'));
form.hidden=true; transcript.hidden=true; info.className='codex-thread-info'; conversation.append(back,empty,info, transcript, form); workspace.append(sidebar, conversation); root.append(heading, unlock, status, workspace);
let requestId = null;
// UUID generation also works on trusted LAN HTTP, where randomUUID is unavailable.
function uuid() { const b = crypto.getRandomValues(new Uint8Array(16)); b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128; return [...b].map((x, i) => ([4,6,8,10].includes(i) ? '-' : '') + x.toString(16).padStart(2, '0')).join(''); }
async function api(path, body) {
  if(!allowedBrowser)throw Error('Open this page through localhost or an SSH tunnel before entering a Codex owner token.');
  const response = await fetch('/api/v1/master/codex/' + path, { method: body ? 'POST' : 'GET', cache: 'no-store', headers: { Authorization: 'Bearer ' + token, ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(20000) });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error?.message || 'Codex connection failed');
  return value;
}
function failure(error) { status.textContent = error.message; status.dataset.state='error'; }
function filterThreads(){const term=search.value.trim().toLocaleLowerCase();const nodes=[...list.querySelectorAll('.codex-thread')]; for(const node of nodes)node.hidden=!node.textContent.toLocaleLowerCase().includes(term); noResults.hidden=!term||!nodes.length||nodes.some(node=>!node.hidden);}
search.oninput=filterThreads;
async function load() {
  const epoch = generation;
  const value = await api('threads?offset=' + offset);
  if (epoch !== generation) return;
  next = value.next_offset; previous.disabled = offset === 0; more.disabled = next === null;
  list.replaceChildren();
  for (const thread of value.threads) {
    const button = el('button', '', 'codex-thread'); button.type = 'button'; button.setAttribute('aria-pressed', String(selected === thread.id));
    button.append(el('strong', thread.name || thread.title || 'Untitled thread'), el('span', (thread.activity === 'running' ? '● Last observed running' : thread.activity === 'idle' ? 'Past turn completed' : 'State unknown') + ' · ' + new Date(thread.updated_at * 1000).toLocaleString()), el('small', thread.cwd));
    button.dataset.activity=thread.activity; button.onclick = () => choose(thread.id).catch(failure); list.append(button);
  }
  if (!value.threads.length) list.append(el('p', 'No local conversations found.'));
  filterThreads();
  status.dataset.state=value.connected?'connected':'disconnected';
  status.textContent = value.machine + (value.connected ? ' · VS Code socket available' : ' · VS Code disconnected');
}
async function choose(id) {
  if (busy) return;
  if (selected !== id && input.value && !window.confirm('Discard the unsent instruction and switch threads?')) return;
  if (selected !== id) { input.value = ''; requestId = null; renderedThread=''; renderedMessages=''; observedActivity=''; }
  workspace.classList.add('codex-reading'); conversation.setAttribute('aria-busy','true');
  selected = id;
  send.disabled = true;
  try { await readThread(); await load(); } finally { conversation.setAttribute('aria-busy','false'); }
}
async function readThread() {
  if (!selected) { form.hidden = true; return; }
  const id = selected, epoch = generation;
  const thread = await api('threads/' + encodeURIComponent(id));
  if (epoch !== generation || selected !== id) return;
  empty.hidden=true; transcript.hidden=false;
  const metadata=JSON.stringify([id,thread.name,thread.title,thread.cwd,thread.model,thread.owner_connected,thread.ide_context,thread.history_limited]);
  if(renderedThread!==metadata){
  info.replaceChildren(el('h2', thread.name || thread.title || 'Untitled thread'), el('p', thread.cwd + ' · ' + (thread.model || 'Thread model'), 'muted'));
  const owner = el('p', thread.owner_connected ? 'Connected to the local VS Code thread' : 'Open this thread in local VS Code to send instructions.'); info.append(owner);
  if (thread.ide_context) { const details = el('details'), summary = el('summary', 'VS Code context'); details.append(summary, el('pre', typeof thread.ide_context === 'string' ? thread.ide_context : JSON.stringify(thread.ide_context, null, 2))); info.append(details); }
  if (thread.history_limited) info.append(el('small', 'Showing the latest 60 messages from the recent history window.'));
  renderedThread=metadata;
  }
  const messageSignature=JSON.stringify(thread.messages);
  const nearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  if(renderedMessages!==messageSignature){
  transcript.replaceChildren();
  for (const message of thread.messages) { const item = el('article', '', 'codex-message ' + message.role); item.append(el('strong', message.role === 'user' ? 'You' : 'Codex'), el('div', message.text)); transcript.append(item); }
  if (!thread.messages.length) transcript.append(el('p', 'No message text is available in the recent history.'));
  renderedMessages=messageSignature;
  if (nearBottom) transcript.scrollTop = transcript.scrollHeight;
  }
  form.hidden = false; send.disabled = !thread.owner_connected || busy;
  if(observedActivity!==thread.activity){mode.value = thread.activity === 'running' ? 'steer' : 'send'; observedActivity=thread.activity;}
  info.dataset.connected=String(thread.owner_connected);
  open.disabled=busy;
}
unlock.onsubmit = async event => {
  event.preventDefault(); token = password.value.trim(); connect.disabled = true;
  try {
    if(secureRemote){const response=await fetch('/session',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:'{}'});if(!response.ok)throw Error('Owner authentication failed');token='';}
    await load(); password.value = ''; unlock.hidden = true; workspace.hidden = false; lock.hidden = false; form.hidden = !selected; }
  catch (error) { token = ''; failure(error); } finally { connect.disabled = false; }
};
lock.onclick = async () => { if(secureRemote){const response=await fetch('/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!response.ok){failure(Error('Could not revoke this device. Try Lock again.'));return;}} generation++; token = ''; selected = null; input.value = ''; requestId = null; list.replaceChildren(); transcript.replaceChildren(); info.replaceChildren(); workspace.hidden = true; workspace.classList.remove('codex-reading'); empty.hidden=false; transcript.hidden=true; form.hidden=true; renderedThread=''; renderedMessages=''; observedActivity=''; search.value=''; lock.hidden = true; unlock.hidden = false; status.dataset.state=''; status.textContent = 'Locked'; };
refresh.onclick = () => { load().then(readThread).catch(failure); };
let newChatRequest = null;
newChat.onclick = async () => {
  if (busy) return;
  if (input.value && !window.confirm('Discard the unsent instruction and start a new chat?')) return;
  busy = true; newChat.disabled = true; lock.disabled = true; send.disabled = true;
  newChatRequest ||= uuid();
  status.textContent = 'Creating conversation…';
  try {
    const result = await api('commands', { action: 'new', request_id: newChatRequest });
    status.textContent = result.message;
    if (result.accepted) {
      selected = result.thread_id; offset = 0; input.value = ''; requestId = null; newChatRequest = null;
      renderedThread = ''; renderedMessages = ''; observedActivity = ''; search.value = '';
      workspace.classList.add('codex-reading');
      await load(); await readThread(); input.focus(); status.textContent = result.message;
    }
  } catch (error) { failure(new Error(error.message + ' Refresh before trying again; the chat may already exist.')); }
  finally { busy = false; newChat.disabled = false; lock.disabled = false; await readThread().catch(failure); }
};
previous.onclick = () => { offset = Math.max(0, offset - 20); load().catch(failure); };
more.onclick = () => { if (next !== null) { offset = next; load().catch(failure); } };
input.oninput = () => { requestId = null; };
form.onsubmit = async event => {
  event.preventDefault(); if (busy || !selected || !input.value.trim()) return;
  busy = true; send.disabled = true; lock.disabled = true; requestId ||= uuid();
  try {
    const result = await api('commands', { thread_id: selected, action: mode.value, text: input.value, request_id: requestId });
    status.textContent = result.message;
    if (result.accepted) { input.value = ''; requestId = null; }
  } catch (error) { failure(new Error(error.message + ' Check the conversation before retrying; delivery may be uncertain.')); }
  finally { busy = false; lock.disabled = false; await readThread().catch(failure); }
};
open.onclick = async () => { if (!selected || busy) return; open.disabled = true; try { const result = await api('commands', { thread_id: selected, action: 'open' }); status.textContent = result.message; } catch (error) { failure(error); } finally { open.disabled = false; } };
async function restoreDevice(){
  if(!secureRemote)return;
  const pair=new URLSearchParams(location.hash.slice(1)).get('pair');
  if(pair){history.replaceState(null,'',location.pathname);try{const response=await fetch('/pair',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:pair})});if(!response.ok)throw Error('This pairing link expired or was already used. Request a fresh one from CompAI.');}catch(error){failure(error);return;}}
  try{await load();unlock.hidden=true;workspace.hidden=false;lock.hidden=false;form.hidden=!selected;}catch(error){if(pair)failure(error);}
}
window.addEventListener('hashchange',()=>void restoreDevice());
void restoreDevice();
let polling = false;
setInterval(async () => { if ((!token && !secureRemote) || workspace.hidden || document.hidden || busy || polling) return; polling = true; try { if (selected) await readThread(); } catch (error) { failure(error); } finally { polling = false; } }, 5000);
