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
// 5 s heartbeat → backend considers the profile stale after 15 s (3 missed beats)
const WS_HEARTBEAT_MS = 5000;
const WS_BACKOFF_MIN_MS = 1000;
const WS_BACKOFF_MAX_MS = 30000;

let ws           = null;
let profileId    = null;
let serverWsBase = null; // e.g. 'ws://192.168.1.13:8000/ws/agent/' — set from ext-init URL
let reconnTimer  = null;
let pingTimer    = null;
let heartbeatTimer = null;
let reconnectAttempts = 0;
let lastWsUrl = '';
const browserSession = crypto.randomUUID();
let websocketId = crypto.randomUUID();
const _incomingQueue = [];
const _ackQueue = [];

function wsBaseFromOrigin(origin) {
  if (!origin) return null;
  try {
    const parsed = new URL(origin);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
    parsed.protocol = parsed.protocol === 'https:' ? 'wss:' : 'ws:';
    parsed.pathname = '/ws/agent/';
    parsed.search = '';
    parsed.hash = '';
    return parsed.toString();
  } catch (_) {
    return null;
  }
}

// ── Keepalive port from content script ───────────────────────────────────────
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== 'keepalive') return;
  port.onMessage.addListener(() => {});
  if (profileId) connect(profileId);
  port.onDisconnect.addListener(() => {});
});

// ── Auto-configure profile ID + server URL from GoLogin startup URL ──────────
// When a GoLogin profile is created via the backend, its startUrl is set to
// http://{BACKEND_URL}/ext-init/{profile_id}/
// We capture the server origin from that URL so the WebSocket always connects
// to the right IP — even when GoLogin blocks 127.0.0.1/localhost.

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === 'complete' && (tab.url || '').startsWith('https://web.whatsapp.com') && profileId) {
    connect(profileId);
  }
  if (info.status !== 'loading') return;
  const url = tab.url || '';
  // Capture any server origin (works for 127.0.0.1, 192.168.x.x, localhost, etc.)
  const match = url.match(/^(https?:\/\/[^/]+)\/ext-init\/([^/?#]+)\/?/);
  if (!match) return;

  const serverOrigin = match[1]; // e.g. 'http://192.168.1.13:8000'
  const pid          = match[2];
  const wsBase       = wsBaseFromOrigin(serverOrigin);

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

  if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
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
  lastWsUrl = url;
  websocketId = crypto.randomUUID();
  const status = reconnectAttempts > 0 ? 'reconnecting' : 'connecting';
  chrome.storage.local.set({ ws_status: status });
  console.log(`[WS-${status === 'reconnecting' ? 'Reconnect' : 'Connect'}] ${url}`);
  const socket = new WebSocket(url);
  ws = socket;

  ws.onopen = async () => {
    console.log('[WS-Connected]', url);
    reconnectAttempts = 0;
    const health = await getProfileHealth();
    ws.send(JSON.stringify({
      type: 'register',
      profile_id: pid,
      websocket_id: websocketId,
      browser_session: browserSession,
      whatsapp_ready: !!health.whatsapp_ready,
      timestamp: new Date().toISOString(),
    }));
    chrome.storage.local.set({ ws_status: 'connected' });
    startPing();
    startHeartbeat();
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
    stopHeartbeat();
    console.warn('[WS-Disconnected] reconnecting soon');
    if (ws === socket) {
      ws = null;
      scheduleReconnect(pid);
    }
  };

  ws.onerror = (e) => {
    console.error('[Agent] WS error', e);
  };
}

function scheduleReconnect(pid) {
  reconnectAttempts += 1;
  const delay = Math.min(WS_BACKOFF_MAX_MS, WS_BACKOFF_MIN_MS * Math.pow(2, reconnectAttempts - 1));
  chrome.storage.local.set({ ws_status: 'reconnecting' });
  console.warn('[WS-Reconnect] attempt=%d delay_ms=%d url=%s', reconnectAttempts, delay, lastWsUrl);
  clearTimeout(reconnTimer);
  reconnTimer = setTimeout(() => connect(pid), delay);
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

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    emitProfileHeartbeat();
  }, WS_HEARTBEAT_MS);
  emitProfileHeartbeat();
}

function stopHeartbeat() {
  if (heartbeatTimer) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

async function emitProfileHeartbeat() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const health = await getProfileHealth();
  const payload = {
    type: 'heartbeat',
    profile_id: profileId || '',
    websocket_id: websocketId,
    browser_session: browserSession,
    whatsapp_ready: !!(health && health.whatsapp_ready),
    extension_connected: true,
    active_chat: (health && health.active_chat) || '',
    url: (health && health.url) || '',
    content_script_ready: !!(health && health.content_script_ready),
    wa_state: (health && health.wa_state) || '',
    timestamp: new Date().toISOString(),
    error: (health && health.error) || '',
  };
  ws.send(JSON.stringify(payload));
  const waState = payload.wa_state || '';
  if (waState === 'ready') {
    console.log('[SessionHealthy] profile_id=%s whatsapp_ready=%s', payload.profile_id, payload.whatsapp_ready);
  } else if (waState === 'qr_required') {
    console.log('[SessionDead] profile_id=%s reason=qr_required', payload.profile_id);
  } else if (waState) {
    console.log('[SessionRecovering] profile_id=%s wa_state=%s', payload.profile_id, waState);
  }
  console.log('[Heartbeat] emitted profile_id=%s whatsapp_ready=%s active_chat=%s',
              payload.profile_id, payload.whatsapp_ready, payload.active_chat);
}

async function getProfileHealth() {
  let health = {
    whatsapp_ready: false,
    active_chat: '',
    url: '',
    content_script_ready: false,
    wa_state: 'no_whatsapp_tab',
  };
  try {
    const tabs = await chrome.tabs.query({ url: 'https://web.whatsapp.com/*' });
    if (tabs.length > 0) {
      health = await chrome.tabs.sendMessage(tabs[0].id, { type: 'GET_PROFILE_HEALTH' });
      health.content_script_ready = true;
    }
  } catch (e) {
    health = { ...health, error: e.message || String(e), wa_state: 'content_script_missing' };
  }
  return health || {};
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
      const isChannelClosed = e.message && (
        e.message.includes('message channel closed') ||
        e.message.includes('listener indicated an asynchronous response')
      );
      result = { success: false, status: isChannelClosed ? 'content_script_missing' : undefined, error: e.message };
    }
    if (attempt < 3) await sleep(2000);
  }
  return result;
}

// Wait until the active WhatsApp conversation in the given tab matches targetJid.
// Polls every 600 ms via GET_ACTIVE_JID message to the content script.
// Returns { matched: bool, activeJid: string }.
async function _waitUntilChatMatchesJid(tabId, targetJid, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 12000);
  const normTarget = normalizeJid(targetJid);
  while (Date.now() < deadline) {
    try {
      const result = await chrome.tabs.sendMessage(tabId, { type: 'GET_ACTIVE_JID' });
      const activeJid = result && result.jid ? normalizeJid(result.jid) : '';
      if (activeJid && activeJid === normTarget) return { matched: true, activeJid };
    } catch (_) {}
    await sleep(600);
  }
  try {
    const result = await chrome.tabs.sendMessage(tabId, { type: 'GET_ACTIVE_JID' });
    const activeJid = result && result.jid ? normalizeJid(result.jid) : '';
    return { matched: activeJid === normTarget, activeJid };
  } catch (_) {
    return { matched: false, activeJid: '' };
  }
}

function ackStatusFromResult(result, fallbackStatus = 'send_failed') {
  if (result && result.success) return 'sent';
  return (result && result.status) || fallbackStatus;
}

// ── Per-task dedup helpers (chrome.storage.local persistence) ─────────────────
// Prevents the same MessageLog from being physically sent twice even when the
// backend retries after an ACK timeout.  Keyed by task/message ID; expires 1 h.

async function _isTaskAlreadySent(taskId) {
  if (!taskId) return false;
  try {
    const stored = await chrome.storage.local.get('sent_task_ids');
    const ids = stored.sent_task_ids || {};
    const ts = ids[String(taskId)];
    return !!(ts && Date.now() - ts < 3600000);
  } catch (_) { return false; }
}

async function _markTaskSent(taskId) {
  if (!taskId) return;
  try {
    const stored = await chrome.storage.local.get('sent_task_ids');
    const ids = stored.sent_task_ids || {};
    ids[String(taskId)] = Date.now();
    // Prune oldest entries when the map grows large
    const keys = Object.keys(ids);
    if (keys.length > 500) {
      keys.sort((a, b) => ids[a] - ids[b]).slice(0, 100).forEach(k => delete ids[k]);
    }
    await chrome.storage.local.set({ sent_task_ids: ids });
  } catch (_) {}
}

async function _executeSend(task) {
  const jid = normalizeJid(task.jid || '');
  task.jid = jid;
  if (!jid) {
    console.warn('[ExtensionTask] failed reason=invalid_jid message_id=%s profile_id=%s',
                 task.message_id || task.task_id, profileId || task.profile_id || '');
    emitAck(task, 'invalid_jid', 'Missing valid JID for send task');
    return;
  }

  // Strict profile isolation: reject any task whose profile_id doesn't match
  // this WebSocket's profile_id.  Prevents cross-profile dispatch.
  const taskProfile = task.profile_id || '';
  if (taskProfile && profileId && taskProfile !== profileId) {
    console.error('[ExtensionTask] failed reason=profile_mismatch task_profile=%s ws_profile=%s jid=%s — dropped',
                  taskProfile, profileId, jid);
    emitAck(task, 'send_failed', `profile_mismatch:task=${taskProfile} ws=${profileId}`);
    return;
  }

  // Per-task dedup: if this task ID was already sent (persisted in chrome.storage),
  // emit a 'sent' ACK immediately so the backend can finalize the log without resending.
  const _taskId = String(task.message_id || task.task_id || '');
  if (_taskId && await _isTaskAlreadySent(_taskId)) {
    console.log('[DuplicateSendPrevented] task_id=%s jid=%s — already sent in this session, emitting ACK',
                _taskId, jid);
    emitAck(task, 'sent', '');
    return;
  }

  const health = await getProfileHealth();
  if (!health.whatsapp_ready) {
    const reason = health.wa_state || health.error || 'whatsapp_not_ready';
    console.warn('[ChatValidationFailed] reason=%s message_id=%s profile_id=%s jid=%s',
                 reason, task.message_id || task.task_id, profileId || task.profile_id || '', jid);
    emitAck(task, health.wa_state === 'content_script_missing' ? 'content_script_missing' : 'whatsapp_not_ready', reason);
    return;
  }
  console.log('[ExtensionTask] received message_id=%s profile_id=%s jid=%s conversation=%s',
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

  // Visual ACK reconciliation: if outgoing bubble already exists for this message,
  // emit a sent ACK immediately — no need to re-send (handles missed WS ACKs).
  {
    const waTabs = await chrome.tabs.query({ url: 'https://web.whatsapp.com/*' });
    if (waTabs.length > 0) {
      try {
        const bubbleCheck = await chrome.tabs.sendMessage(waTabs[0].id, {
          type: 'GET_LATEST_OUTGOING', jid, message: task.message || '',
        });
        if (bubbleCheck && bubbleCheck.found) {
          console.log('[ExistingBubbleDetected] message_id=%s jid=%s — outgoing bubble found, emitting sent ACK',
                      task.message_id || task.task_id, jid);
          await _markTaskSent(_taskId);
          emitAck(task, 'sent', '');
          return;
        }
      } catch (_) {}
    }
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

  // Tell content.js to suppress incoming processing for this JID while we send.
  // Prevents the opened chat from re-entering the auto-reply pipeline via the
  // sidebar scanner or active-conversation poll for the next 5 minutes.
  try {
    await chrome.tabs.sendMessage(tabId, {
      type: 'SUPPRESS_INCOMING_JID',
      jid,
      ttl: 300000,
    });
  } catch (_) {}

  try {
    await chrome.tabs.update(tabId, { url });
    await waitForTabLoad(tabId, 20000);
  } catch (e) {
    emitAck(task, 'open_chat_failed', e.message || String(e));
    return;
  }
  // Validate opened_jid === target_jid before sending — never send blindly.
  const { matched, activeJid } = await _waitUntilChatMatchesJid(tabId, jid, 12000);
  console.log('[OpenChat] wait complete - message_id=%s jid=%s matched=%s active_jid=%s',
              task.message_id || task.task_id, jid, matched, activeJid || 'none');
  if (!matched) {
    console.error('[RoutingMismatch] expected_jid=%s actual_jid=%s profile_id=%s task_id=%s — aborting send',
                  jid, activeJid || 'none', profileId || '', task.message_id || task.task_id);
    emitAck(task, 'routing_mismatch', `routing_mismatch:expected=${jid} actual=${activeJid || 'none'}`);
    return;
  }

  let result = { success: false, error: 'Content script did not respond after retries' };
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      emitStage(task, 'sending');
      console.log('[SendTyped] attempt=%d message_id=%s profile_id=%s jid=%s',
                  attempt, task.message_id || task.task_id, profileId || task.profile_id || '', jid);
      result = await chrome.tabs.sendMessage(tabId, {
        type:    'CLICK_SEND',
        task_id: task.message_id || task.task_id,
        message: task.message || '',
        jid,
      });
      if (result && result.success) {
        console.log('[SendTyped] verified message_id=%s jid=%s attempt=%d',
                    task.message_id || task.task_id, jid, attempt);
        break;
      }
    } catch (e) {
      const isChannelClosed = e.message && (
        e.message.includes('message channel closed') ||
        e.message.includes('listener indicated an asynchronous response')
      );
      result = { success: false, status: isChannelClosed ? 'content_script_missing' : undefined, error: e.message };
    }
    if (attempt < 3) await sleep(3000);
  }

  const ackStatus = ackStatusFromResult(result);
  console.log('[ACK] emitting message_id=%s jid=%s status=%s', task.message_id || task.task_id, jid, ackStatus);
  if (ackStatus === 'sent') {
    await _markTaskSent(_taskId);
  }
  emitAck(task, ackStatus, result && result.error ? result.error : '');
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
  const ok = status === 'sent';
  const payload = {
    type:       ok ? 'message_sent_ack' : 'message_failed_ack',
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
    const nextWsBase = wsBaseFromOrigin(msg.server_origin);
    console.log('[Agent] Received profile_id from init.js content script:', pid, '| server:', msg.server_origin || 'unknown');
    const stored = { profile_id: pid, ws_status: 'connecting' };
    if (nextWsBase) {
      stored.server_ws_base = nextWsBase;
      serverWsBase = nextWsBase;
    }
    chrome.storage.local.set(stored);
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
