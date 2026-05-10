/**
 * Service worker — runs in the background inside each GoLogin Chrome profile.
 *
 * Keepalive strategy (Chrome MV3):
 *  - Web Locks API → keeps SW alive indefinitely (primary, no tab needed)
 *  - Content script opens a port → secondary keepalive while WhatsApp tab is open
 *  - WebSocket ping every 20s → keeps SW active via event callbacks
 *  - chrome.alarms every 1 min → wakes SW if killed, reconnects WebSocket
 *
 * Server URL strategy:
 *  - GoLogin blocks ALL loopback addresses (127.x.x.x / localhost) via its
 *    internal network proxy. We derive the real server URL dynamically from
 *    the ext-init startUrl (e.g. http://192.168.1.13:8000/ext-init/{pid}/)
 *    and store it in chrome.storage as 'server_ws_base'.
 */

// ── Primary keepalive: Web Locks API ─────────────────────────────────────────
if (navigator.locks) {
  navigator.locks.request('sw-keepalive', { mode: 'shared' }, () => new Promise(() => {}));
}

const KEEPALIVE_ALARM = 'ws-keepalive';

let ws           = null;
let profileId    = null;
let serverWsBase = null; // e.g. 'ws://192.168.1.13:8000/ws/agent/' — set from ext-init URL
let reconnTimer  = null;
let pingTimer    = null;
const _incomingQueue = [];
const _ackQueue = [];

// ── Keepalive port from content script ───────────────────────────────────────
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'keepalive') return;
  port.onMessage.addListener(() => {});
  port.onDisconnect.addListener(() => {});
});

// ── Auto-configure profile ID + server URL from GoLogin startup URL ──────────
// When a GoLogin profile is created via the backend, its startUrl is set to
// http://{BACKEND_URL}/ext-init/{profile_id}/
// We capture the server origin from that URL so the WebSocket always connects
// to the right IP — even when GoLogin blocks 127.0.0.1/localhost.

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status !== 'loading') return;
  const url = tab.url || '';
  // Capture any server origin (works for 127.0.0.1, 192.168.x.x, localhost, etc.)
  const match = url.match(/^(https?:\/\/[^/]+)\/ext-init\/([^/?#]+)\/?/);
  if (!match) return;

  const serverOrigin = match[1]; // e.g. 'http://192.168.1.13:8000'
  const pid          = match[2];
  const wsBase       = serverOrigin.replace(/^http/, 'ws') + '/ws/agent/';

  console.log('[Agent] Auto-configuring profile ID from startUrl:', pid, '| server:', serverOrigin);

  chrome.storage.local.set({ profile_id: pid, server_ws_base: wsBase, ws_status: 'connecting' }, () => {
    profileId    = pid;
    serverWsBase = wsBase;
    if (ws) { try { ws.close(); } catch (_) {} }
    connect(pid);
    // Redirect to WhatsApp Web
    chrome.tabs.update(tabId, { url: 'https://web.whatsapp.com' });
  });
});

// ── Bootstrap ────────────────────────────────────────────────────────────────

async function init() {
  await _ensureOffscreen();

  const stored = await chrome.storage.local.get(['profile_id', 'server_ws_base']);
  profileId    = stored.profile_id    || null;
  serverWsBase = stored.server_ws_base || null;

  if (!profileId) {
    profileId = await detectProfileId();
  }

  if (profileId) {
    connect(profileId);
  } else {
    chrome.storage.local.set({ ws_status: 'disconnected' });
    console.warn('[Agent] No profile_id — will retry via alarm.');
  }

  chrome.alarms.create(KEEPALIVE_ALARM, { periodInMinutes: 0.5 });
}

// ── Auto-detect profile ID via cookie or server API ──────────────────────────

async function detectProfileId() {
  // 1. Try cookie — use the stored server origin so we look on the right domain
  try {
    const s = await chrome.storage.local.get('server_ws_base');
    const cookieOrigin = s.server_ws_base
      ? s.server_ws_base.replace(/^ws/, 'http').replace('/ws/agent/', '')
      : 'http://127.0.0.1:8000';
    const cookie = await chrome.cookies.get({
      url:  cookieOrigin + '/',
      name: 'gl_profile_id',
    });
    if (cookie && cookie.value) {
      console.log('[Agent] Recovered profile_id from cookie:', cookie.value);
      await chrome.storage.local.set({ profile_id: cookie.value });
      return cookie.value;
    }
  } catch (e) {
    console.warn('[Agent] Cookie read failed:', e.message);
  }

  // 2. Fallback: ask the backend to match this browser's User-Agent to a profile
  return await detectProfileIdFromServer();
}

let _serverDetectAttempts = 0;

async function detectProfileIdFromServer() {
  if (_serverDetectAttempts >= 5) return null;
  _serverDetectAttempts++;

  // Derive HTTP server base from stored WS base, or fall back to 127.0.0.1
  const s = await chrome.storage.local.get('server_ws_base');
  const serverBase = s.server_ws_base
    ? s.server_ws_base.replace(/^ws/, 'http').replace('/ws/agent/', '')
    : 'http://127.0.0.1:8000';

  try {
    const resp = await fetch(`${serverBase}/api/profiles/detect/`);
    if (resp.ok) {
      const data = await resp.json();
      if (data.profile_id) {
        console.log('[Agent] Detected profile_id from server API:', data.profile_id);
        await chrome.storage.local.set({ profile_id: data.profile_id });
        _serverDetectAttempts = 0;
        return data.profile_id;
      }
    }
  } catch (e) {
    console.warn('[Agent] Server detect failed:', e.message);
  }
  return null;
}

async function _ensureOffscreen() {
  try {
    if (!chrome.offscreen) return;
    const hasDoc = await chrome.offscreen.hasDocument();
    if (!hasDoc) {
      await chrome.offscreen.createDocument({
        url:           chrome.runtime.getURL('offscreen.html'),
        reasons:       ['DOM_SCRAPING'],
        justification: 'Persistent keepalive port — prevents service worker idle kill',
      });
    }
  } catch (_) {
    // Non-fatal — Web Locks and content-script port are still active
  }
}

// ── Alarm handler — reconnect if dead ────────────────────────────────────────

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== KEEPALIVE_ALARM) return;

  if (!profileId) {
    profileId = await detectProfileId();
    if (profileId) connect(profileId);
    return;
  }

  if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CONNECTING) {
    clearTimeout(reconnTimer);
    connect(profileId);
  }
});

// ── WebSocket ────────────────────────────────────────────────────────────────

function connect(pid) {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  clearTimeout(reconnTimer);

  // Use the dynamically discovered server URL; fall back to 127.0.0.1 for
  // regular Chrome where loopback works fine.
  const wsBase = serverWsBase || 'ws://127.0.0.1:8000/ws/agent/';
  const url    = `${wsBase}${pid}/`;
  console.log(`[Agent] Connecting to ${url}`);
  const socket = new WebSocket(url);
  ws = socket;

  ws.onopen = () => {
    console.log('[Agent] WebSocket connected');
    ws.send(JSON.stringify({ type: 'register', profile_id: pid }));
    chrome.storage.local.set({ ws_status: 'connected' });
    startPing();
    flushAckQueue();
    flushIncomingQueue();
    // Immediately scan for any messages that arrived while disconnected
    setTimeout(() => {
      chrome.tabs.query({ url: 'https://web.whatsapp.com/*' }, (tabs) => {
        if (tabs.length > 0) {
          chrome.tabs.sendMessage(tabs[0].id, { type: 'SCAN_NOW' }, () => {
            void chrome.runtime.lastError;
          });
        }
      });
    }, 2000);
  };

  ws.onmessage = (event) => {
    let msg;
    try { msg = JSON.parse(event.data); } catch (_) { return; }

    if (msg.type === 'send_message_by_jid') {
      handleSendTask(msg);
    } else if (msg.type === 'check_status') {
      reportWaStatus();
    } else if (msg.type === 'pong') {
      // server acknowledged ping — connection healthy
    }
  };

  ws.onclose = () => {
    stopPing();
    chrome.storage.local.set({ ws_status: 'disconnected' });
    console.warn('[Agent] WS closed — reconnecting in 3 s');
    if (ws === socket) {
      ws = null;
      reconnTimer = setTimeout(() => connect(pid), 3000);
    }
  };

  ws.onerror = (e) => {
    console.error('[Agent] WS error', e);
  };
}

// ── Ping — keeps service worker alive via WebSocket event callbacks ───────────

function startPing() {
  stopPing();
  pingTimer = setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'ping' }));
    }
  }, 20000);
}

function stopPing() {
  if (pingTimer) {
    clearInterval(pingTimer);
    pingTimer = null;
  }
}

// ── Serial send queue ─────────────────────────────────────────────────────────

const _sendQueue = [];
let   _queueBusy = false;

function handleSendTask(task) {
  console.log('[Agent] Task received - type=%s message_id=%s profile_id=%s jid=%s',
              task.type, task.message_id || task.task_id, task.profile_id || profileId || '', task.jid || '');
  _sendQueue.push(task);
  if (!_queueBusy) _drainQueue();
}

async function _drainQueue() {
  if (_queueBusy) return;
  _queueBusy = true;
  while (_sendQueue.length > 0) {
    const task = _sendQueue.shift();
    try {
      await _executeSend(task);
    } catch (e) {
      console.error('[Agent] Unhandled error for task', task.task_id, e);
      emitAck(task, 'send_failed', String(e));
    }
  }
  _queueBusy = false;
}


function normalizeJid(value) {
  const raw = String(value || '').toLowerCase().trim();
  const match = raw.match(/(\d{7,20}|120363\d{5,})@(c\.us|g\.us)/);
  return match ? `${match[1]}@${match[2]}` : '';
}

async function sendByJidInContent(jid, message, taskId) {
  const waTabs = await chrome.tabs.query({ url: 'https://web.whatsapp.com/*' });
  if (waTabs.length === 0) {
    return { success: false, error: 'No WhatsApp tab open for JID send' };
  }
  let result = { success: false, error: 'Content script did not respond' };
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      result = await chrome.tabs.sendMessage(waTabs[0].id, {
        type: 'SEND_BY_JID',
        jid,
        message,
        task_id: taskId,
      });
      if (result && result.success) break;
    } catch (e) {
      result = { success: false, error: e.message };
    }
    if (attempt < 3) await sleep(2000);
  }
  return result;
}

function ackStatusFromResult(result, fallbackStatus = 'send_failed') {
  if (result && result.success) return 'sent';
  return (result && result.status) || fallbackStatus;
}

async function _executeSend(task) {
  const jid = normalizeJid(task.jid || '');
  task.jid = jid;
  if (!jid) {
    console.warn('[Extension] invalid_jid - message_id=%s profile_id=%s jid=%s',
                 task.message_id || task.task_id, profileId || task.profile_id || '', task.jid || '');
    emitAck(task, 'invalid_jid', 'Missing valid JID for send task');
    return;
  }
  console.log('[Extension] task received - message_id=%s profile_id=%s jid=%s conversation=%s',
              task.message_id || task.task_id, profileId || task.profile_id || '', jid, task.conversation_id || '');
  emitStage(task, 'extension_received');

  if (jid.endsWith('@g.us')) {
    emitStage(task, 'opening_chat');
    emitStage(task, 'sending');
    const result = await sendByJidInContent(jid, task.message, task.message_id || task.task_id);
    emitAck(task, ackStatusFromResult(result), result && result.error ? result.error : '');
    return;
  }

  let phone = task.phone || '';
  if (!phone) {
    const jidPhone = (jid.split('@')[0] || '').replace(/\D/g, '');
    if (jidPhone.length >= 7) {
      phone = jidPhone;
      console.log('[Agent] [Routing] Phone derived from JID:', phone, '| jid:', jid,
                  '| conversation:', task.conversation_id || 'n/a');
    }
  }

  console.log('[Agent] [Send] target=', jid,
              '| phone=', phone || 'none',
              '| log=', task.task_id,
              '| queue=', _sendQueue.length);

  if (!phone) {
    emitAck(task, 'invalid_jid', 'Could not derive phone from JID');
    return;
  }

  // JID-based direct-chat send: navigate phone deep link + click Send.
  let tabs = await chrome.tabs.query({ url: 'https://web.whatsapp.com/*' });
  let tabId;

  if (tabs.length > 0) {
    tabId = tabs[0].id;
  } else {
    const newTab = await chrome.tabs.create({ url: 'https://web.whatsapp.com', active: false });
    tabId = newTab.id;
    await waitForTabLoad(tabId, 15000);
    await sleep(3000);
  }

  const cleanPhone = phone.replace(/\D/g, '');
  const url = `https://web.whatsapp.com/send?phone=${cleanPhone}&text=${encodeURIComponent(task.message)}`;
  console.log('[OpenChat] attempt - message_id=%s profile_id=%s jid=%s phone=%s conversation=%s',
              task.message_id || task.task_id, profileId || task.profile_id || '', jid, cleanPhone, task.conversation_id || '');
  emitStage(task, 'opening_chat');
  try {
    await chrome.tabs.update(tabId, { url });
    await waitForTabLoad(tabId, 20000);
  } catch (e) {
    emitAck(task, 'open_chat_failed', e.message || String(e));
    return;
  }
  await sleep(8000);
  console.log('[OpenChat] wait complete - message_id=%s jid=%s', task.message_id || task.task_id, jid);

  let result = { success: false, error: 'Content script did not respond after retries' };
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      emitStage(task, 'sending');
      console.log('[SendMessage] attempt - message_id=%s profile_id=%s jid=%s attempt=%d',
                  task.message_id || task.task_id, profileId || task.profile_id || '', jid, attempt);
      result = await chrome.tabs.sendMessage(tabId, {
        type:    'CLICK_SEND',
        task_id: task.message_id || task.task_id,
        message: task.message || '',
        jid,
      });
      if (result && result.success) {
        console.log('[SendMessage] verified success - message_id=%s jid=%s attempt=%d',
                    task.message_id || task.task_id, jid, attempt);
        break;
      }
    } catch (e) {
      result = { success: false, error: e.message };
    }
    if (attempt < 3) await sleep(3000);
  }

  emitAck(task, ackStatusFromResult(result), result && result.error ? result.error : '');
}

function emitStage(task, stage, error = '') {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      type:       'message_send_status',
      message_id: task.message_id || task.task_id,
      task_id:    task.task_id,
      jid:        task.jid || '',
      profile_id: profileId || task.profile_id || '',
      stage,
      error,
    }));
    console.log('[Agent] Stage emitted - message_id=%s jid=%s stage=%s',
                task.message_id || task.task_id, task.jid || '', stage);
  }
}

function emitAck(task, status, error = '') {
  const payload = {
    type:       'message_sent_ack',
    message_id: task.message_id || task.task_id,
    task_id:    task.task_id,
    jid:        task.jid || '',
    profile_id: profileId || task.profile_id || '',
    status,
    error,
  };
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(payload));
    console.log('[ACK] emitted - message_id=%s profile_id=%s jid=%s status=%s error=%s',
                payload.message_id, payload.profile_id, payload.jid, status, error || '');
    return;
  }
  _ackQueue.push(payload);
  if (_ackQueue.length > 100) _ackQueue.splice(0, _ackQueue.length - 100);
  console.warn('[ACK] queued - websocket_disconnected message_id=%s jid=%s status=%s',
               payload.message_id, payload.jid, status);
}

function flushAckQueue() {
  if (!_ackQueue.length || !ws || ws.readyState !== WebSocket.OPEN) return;
  while (_ackQueue.length && ws && ws.readyState === WebSocket.OPEN) {
    const payload = _ackQueue.shift();
    ws.send(JSON.stringify(payload));
    console.log('[ACK] flushed - message_id=%s profile_id=%s jid=%s status=%s',
                payload.message_id, payload.profile_id, payload.jid, payload.status);
  }
}

async function reportWaStatus() {
  const tabs     = await chrome.tabs.query({ url: 'https://web.whatsapp.com/*' });
  const waStatus = tabs.length > 0 ? 'open' : 'closed';
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'status_report', wa_status: waStatus }));
  }
}

// ── Incoming messages forwarded from content scripts ─────────────────────────

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg.type === 'SET_PROFILE_ID' && msg.profile_id) {
    const pid = msg.profile_id;
    console.log('[Agent] Received profile_id from init.js content script:', pid);
    chrome.storage.local.set({ profile_id: pid });
    profileId = pid;
    if (ws) { try { ws.close(); } catch (_) {} }
    connect(pid);
    if (sender.tab && sender.tab.id) {
      chrome.tabs.update(sender.tab.id, { url: 'https://web.whatsapp.com' });
    }
  } else if (msg.type === 'INCOMING_MESSAGES') {
    queueIncomingMessages(msg.messages || []);
  } else if (msg.type === 'AGENT_DEBUG') {
    reportDebug(msg.message || '', msg.data || {});
  }
});

// ── Storage change listener ───────────────────────────────────────────────────

function queueIncomingMessages(messages) {
  if (!messages.length) return;

  console.log('[Agent] Received from content script:', messages.length, 'message(s) —',
    messages.map(m => (m.sender_name || '?') + ' src=' + (m.source || '?')).join(', '));
  _incomingQueue.push(...messages);
  console.log('[Agent] Queue size:', _incomingQueue.length, '| WS state:', ws ? ['CONNECTING','OPEN','CLOSING','CLOSED'][ws.readyState] : 'null');
  if (_incomingQueue.length > 100) {
    _incomingQueue.splice(0, _incomingQueue.length - 100);
  }

  flushIncomingQueue();
}

function reportDebug(message, data) {
  console.log('[AgentDebug]', message, data);
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      type:    'agent_debug',
      message: message,
      data:    data,
    }));
  }
}

function flushIncomingQueue() {
  if (_incomingQueue.length === 0) return;

  const wsState = ws ? (['CONNECTING','OPEN','CLOSING','CLOSED'][ws.readyState] || ws.readyState) : 'null';
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn('[Agent] Cannot flush — WS state:', wsState, '| queued:', _incomingQueue.length);
    return;
  }

  const batch = _incomingQueue.splice(0, _incomingQueue.length);
  console.log('[Agent] WS-Send incoming_messages — count:', batch.length, '| senders:',
    batch.map(m => (m.sender_name || '?') + (m.sender_phone ? '(' + m.sender_phone + ')' : '')).join(', '));
  ws.send(JSON.stringify({
    type:     'incoming_messages',
    messages: batch,
  }));
  console.log('[Agent] Forwarded incoming message batch:', batch.length);
}

chrome.storage.onChanged.addListener((changes) => {
  if (changes.server_ws_base?.newValue) {
    serverWsBase = changes.server_ws_base.newValue;
  }
  if (changes.profile_id?.newValue) {
    profileId = changes.profile_id.newValue;
  }
});

// ── Helpers ───────────────────────────────────────────────────────────────────

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function waitForTabLoad(tabId, timeout) {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, timeout);
    function listener(id, info) {
      if (id === tabId && info.status === 'complete') {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

// ── Start ─────────────────────────────────────────────────────────────────────
init();
