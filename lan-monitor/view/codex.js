const root = document.querySelector('#codex-context');
const secureRemote=root.dataset.remote==='true' && location.protocol==='https:';
const allowedBrowser=secureRemote || ['localhost','127.0.0.1','[::1]'].includes(location.hostname);
const el = (tag, text, cls) => { const node = document.createElement(tag); if (text) node.textContent = text; if (cls) node.className = cls; return node; };
let token = '', selected = null, offset = 0, next = null, busy = false, generation = 0;
const heading = el('div', '', 'panel-heading');
const title = el('div'); title.append(el('h2', secureRemote ? 'CompAI · Codex' : 'Local VS Code · Codex'), el('p', secureRemote ? 'Read conversations and send instructions to Codex on CompAI.' : 'Read conversations and send instructions to Codex on this machine.', 'muted'));
const lock = el('button', 'Lock'); lock.hidden = true; heading.append(title, lock);
const unlock = el('form', '', 'codex-unlock');
const label = el('label', 'Codex owner token '), password = el('input'); password.type = 'password'; password.autocomplete = 'off'; password.required = true; label.append(password);
const connect = el('button', 'Connect'); connect.type = 'submit'; unlock.append(label, connect, el('small', secureRemote ? 'Private control of CompAI. Your owner token stays in this tab until you lock or close it.' : 'Use localhost or an SSH tunnel. This separate owner token is never shared with test workers.'));
if(!allowedBrowser){password.disabled=true;connect.disabled=true;}
const status = el('p'); status.setAttribute('role', 'status');
const workspace = el('div', '', 'codex-workspace'); workspace.hidden = true;
const sidebar = el('div', '', 'codex-sidebar'), list = el('div', '', 'codex-list'), controls = el('div', '', 'codex-pagination');
const previous = el('button', 'Newer'), more = el('button', 'Older'), refresh = el('button', 'Refresh'); controls.append(previous, more, refresh); sidebar.append(list, controls);
const conversation = el('div', '', 'codex-conversation'), info = el('div'), transcript = el('div', '', 'codex-transcript'); transcript.tabIndex = 0; transcript.setAttribute('aria-label', 'Conversation messages');
const form = el('form', '', 'codex-compose'), promptLabel = el('label', 'Instruction to the selected thread'), input = el('textarea'); input.rows = 3; input.maxLength = 16000; input.required = true; promptLabel.append(input);
const actions = el('div', '', 'codex-actions'), mode = el('select'); mode.setAttribute('aria-label', 'Instruction delivery');
for (const [value, text] of [['send', 'Send a new message'], ['steer', 'Steer the running turn']]) { const option = el('option', text); option.value = value; mode.append(option); }
const send = el('button', 'Send instruction'); send.type = 'submit'; const open = el('button', 'Open in local VS Code'); open.type = 'button'; actions.append(mode, send, open); form.append(promptLabel, actions, el('small', 'Uses this thread’s existing permissions. Handle approval requests in local VS Code.'));
conversation.append(info, transcript, form); workspace.append(sidebar, conversation); root.append(heading, unlock, status, workspace);
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
function failure(error) { status.textContent = error.message; }
async function load() {
  const epoch = generation;
  const value = await api('threads?offset=' + offset);
  if (epoch !== generation) return;
  next = value.next_offset; previous.disabled = offset === 0; more.disabled = next === null;
  list.replaceChildren();
  for (const thread of value.threads) {
    const button = el('button', '', 'codex-thread'); button.type = 'button'; button.setAttribute('aria-pressed', String(selected === thread.id));
    button.append(el('strong', thread.name || thread.title || 'Untitled thread'), el('span', (thread.activity === 'running' ? '● Last observed running' : thread.activity === 'idle' ? 'Past turn completed' : 'State unknown') + ' · ' + new Date(thread.updated_at * 1000).toLocaleString()), el('small', thread.cwd));
    button.onclick = () => choose(thread.id).catch(failure); list.append(button);
  }
  if (!value.threads.length) list.append(el('p', 'No local conversations found.'));
  status.textContent = value.machine + (value.connected ? ' · VS Code socket available' : ' · VS Code disconnected');
}
async function choose(id) {
  if (busy) return;
  if (selected !== id && input.value && !window.confirm('Discard the unsent instruction and switch threads?')) return;
  if (selected !== id) { input.value = ''; requestId = null; }
  selected = id;
  send.disabled = true;
  await readThread();
  await load();
}
async function readThread() {
  if (!selected) { form.hidden = true; return; }
  const id = selected, epoch = generation;
  const thread = await api('threads/' + encodeURIComponent(id));
  if (epoch !== generation || selected !== id) return;
  info.replaceChildren(el('h3', thread.name || thread.title || 'Untitled thread'), el('p', thread.cwd + ' · ' + (thread.model || 'Thread model'), 'muted'));
  const owner = el('p', thread.owner_connected ? 'Connected to the local VS Code thread' : 'Open this thread in local VS Code to send instructions.'); info.append(owner);
  if (thread.ide_context) { const details = el('details'), summary = el('summary', 'VS Code context'); details.append(summary, el('pre', typeof thread.ide_context === 'string' ? thread.ide_context : JSON.stringify(thread.ide_context, null, 2))); info.append(details); }
  const nearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  transcript.replaceChildren();
  for (const message of thread.messages) { const item = el('article', '', 'codex-message ' + message.role); item.append(el('strong', message.role === 'user' ? 'You' : 'Codex'), el('div', message.text)); transcript.append(item); }
  if (!thread.messages.length) transcript.append(el('p', 'No message text is available in the recent history.'));
  if (thread.history_limited) info.append(el('small', 'Showing the latest 60 messages from the recent history window.'));
  if (nearBottom) transcript.scrollTop = transcript.scrollHeight;
  form.hidden = false; send.disabled = !thread.owner_connected || busy;
  mode.value = thread.activity === 'running' ? 'steer' : 'send';
}
unlock.onsubmit = async event => {
  event.preventDefault(); token = password.value.trim(); connect.disabled = true;
  try { await load(); password.value = ''; unlock.hidden = true; workspace.hidden = false; lock.hidden = false; form.hidden = !selected; }
  catch (error) { token = ''; failure(error); } finally { connect.disabled = false; }
};
lock.onclick = () => { generation++; token = ''; selected = null; input.value = ''; requestId = null; list.replaceChildren(); transcript.replaceChildren(); info.replaceChildren(); workspace.hidden = true; lock.hidden = true; unlock.hidden = false; status.textContent = 'Locked'; };
refresh.onclick = () => { load().then(readThread).catch(failure); };
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
let polling = false;
setInterval(async () => { if (!token || document.hidden || busy || polling) return; polling = true; try { if (selected) await readThread(); } catch (error) { failure(error); } finally { polling = false; } }, 5000);
