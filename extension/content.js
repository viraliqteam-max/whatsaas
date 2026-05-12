/**
 * Content script — injected into every https://web.whatsapp.com/* page.
 *
 * Responsibilities:
 *  1. Keep the background service worker alive (port-based heartbeat)
 *  2. Listen for CLICK_SEND from the background and click the WhatsApp send button
 *  3. Scan the sidebar for unread chats and report them (with phone numbers) to Django
 */

// ── Service worker keepalive ──────────────────────────────────────────────────

let _keepAlivePort = null;

function connectKeepAlive() {
  try {
    _keepAlivePort = chrome.runtime.connect({ name: 'keepalive' });
    _keepAlivePort.onDisconnect.addListener(() => {
      setTimeout(connectKeepAlive, 1000);
    });
    setInterval(() => {
      try { _keepAlivePort.postMessage({ type: 'heartbeat' }); } catch (_) {}
    }, 5000);
  } catch (_) {}
}

connectKeepAlive();

// ── Message sending ───────────────────────────────────────────────────────────

const SEND_SELECTORS = [
  '[data-testid="compose-btn-send"]',
  '[data-testid="send"]',
  'span[data-icon="send"]',
  'button[aria-label="Send"]',
  'button[aria-label="send"]',
];

// Business interstitial "Continue / OK" button selectors
const CONTINUE_SELECTORS = [
  '[data-testid="popup-controls-ok"]',
  '[data-testid="confirm"]',
  '[data-testid="continue"]',
  '[data-testid="open-chat"]',
  '[data-testid="startup-screens-continue"]',
  'button[aria-label="Continue"]',
  'button[aria-label="OK"]',
  'button[aria-label="Got it"]',
  'button[aria-label="Message"]',
];

// Catalog / greeting popup close selectors
const CLOSE_SELECTORS = [
  '[data-testid="x-viewer"]',
  '[data-testid="close"]',
  '[data-testid="modal-close-button"]',
  'button[aria-label="Close"]',
  'button[aria-label="close"]',
];

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === 'CLICK_SEND') {
    console.log('[Agent] Content task received - CLICK_SEND jid=%s task=%s',
                msg.jid || '', msg.task_id || '');
    clickSendButton(msg.message || '')
      .then(result => sendResponse(result))
      .catch(e   => sendResponse({ success: false, error: e.message }));
    return true;
  }
  if (msg.type === 'SCAN_NOW') {
    // Wait for WhatsApp to be ready before scanning (deep-link navigations
    // trigger SCAN_NOW before the chat list has rendered)
    _waitForWhatsApp(10000).then(() => {
      const messages = scanUnreadChats();
      _debug('scan_now_complete', { count: messages.length });
      if (messages.length > 0) {
        chrome.runtime.sendMessage({ type: 'INCOMING_MESSAGES', messages }, () => {
          void chrome.runtime.lastError;
        });
      }
    });
    return;
  }
  if (msg.type === 'FIND_PHONE') {
    sendResponse({ phone: _findPhoneByName(msg.sender_name) });
    return;
  }
  if (msg.type === 'GET_PROFILE_HEALTH') {
    sendResponse(_getProfileHealth());
    return;
  }
  if (msg.type === 'GET_ACTIVE_JID') {
    sendResponse({ jid: _activeChatJid() });
    return true;
  }
  if (msg.type === 'GET_LATEST_OUTGOING') {
    const latest = _latestOutgoingMessageText();
    const found = _messageTextMatches(latest, msg.message || '');
    if (found) console.log('[ExistingBubbleDetected] jid=%s — outgoing bubble matches pending send', msg.jid || '');
    sendResponse({ found, latest: latest.slice(0, 80) });
    return true;
  }
  if (msg.type === 'SEND_BY_JID') {
    console.log('[Agent] Content task received - SEND_BY_JID jid=%s task=%s', msg.jid || '', msg.task_id || '');
    sendByJid(msg.jid, msg.message)
      .then(result => sendResponse(result))
      .catch(e => sendResponse({ success: false, error: e.message }));
    return true;
  }
  if (msg.type === 'SUPPRESS_INCOMING_JID') {
    const jid = msg.jid || '';
    const ttl = msg.ttl || 300000;
    if (jid) {
      _suppressedJids.set(jid, Date.now() + ttl);
      console.log('[SuppressIncoming] jid=%s ttl=%dms — suppressing incoming pipeline for send target', jid, ttl);
    }
    sendResponse({ ok: true });
    return true;
  }
});

function _isWhatsAppReady() {
  return _getProfileHealth().whatsapp_ready;
}

function _getProfileHealth() {
  const hasSidebar = !!(
    document.querySelector('#pane-side') ||
    document.querySelector('[aria-label="Chat list"]') ||
    document.querySelector('[aria-label="Chats"]') ||
    document.querySelector('[data-testid="chatlist"]')
  );
  const qrScreen = !!document.querySelector('[data-testid="qrcode"], canvas[aria-label*="Scan"], div[data-ref]');
  const loadingScreen = !!document.querySelector('[data-testid="startup-screen"], progress, [aria-label*="Loading"]');
  const disconnected = /computer not connected|phone not connected|trying to reach phone|connecting/i.test(document.body.innerText || '');
  const whatsappReady = hasSidebar && !qrScreen && !loadingScreen && !disconnected;
  const waState = qrScreen ? 'qr_required' :
    loadingScreen ? 'loading' :
    disconnected ? 'disconnected' :
    hasSidebar ? 'ready' :
    'dom_not_ready';
  if (waState === 'ready') {
    console.log('[SessionHealthy] wa_state=ready active_chat=%s', _activeChatName() || '');
  } else if (waState === 'qr_required') {
    console.log('[SessionDead] wa_state=qr_required — session needs QR rescan');
  } else {
    console.log('[SessionRecovering] wa_state=%s', waState);
  }
  return {
    whatsapp_ready: whatsappReady,
    active_chat: _activeChatName(),
    url: location.href,
    content_script_ready: true,
    wa_state: waState,
  };
}

function _normaliseMessageText(text) {
  return String(text || '').replace(/\s+/g, ' ').trim();
}

function _messageTextMatches(actual, expected) {
  actual = _normaliseMessageText(actual);
  expected = _normaliseMessageText(expected);
  if (!actual || !expected) return false;
  return actual === expected || actual.includes(expected) || expected.includes(actual);
}

function _latestOutgoingMessageText() {
  const main = document.querySelector('#main') || document;
  const outgoing = Array.from(main.querySelectorAll('.message-out'))
    .filter(el => !(el.classList && el.classList.contains('message-in')) && !(el.closest && el.closest('.message-in')));

  for (let i = outgoing.length - 1; i >= 0; i--) {
    const el = outgoing[i];
    const textEl = el.querySelector('.selectable-text, span.selectable-text, [data-pre-plain-text]');
    const raw = (textEl ? textEl.innerText || textEl.textContent : el.innerText || el.textContent || '').trim();
    const lines = raw.split('\n').map(x => x.trim()).filter(Boolean);
    const text = lines.find(x =>
      x.length > 0 &&
      x.length < 4000 &&
      !/^\d{1,2}:\d{2}\s*(am|pm)?$/i.test(x) &&
      !/^(read|delivered|sent)$/i.test(x)
    );
    if (text) return text;
  }
  return '';
}

async function _verifyOutgoingMessage(expectedMessage, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 15000);
  while (Date.now() < deadline) {
    const latest = _latestOutgoingMessageText();
    if (_messageTextMatches(latest, expectedMessage)) {
      console.log('[Agent] Send verification success - outgoing bubble matched');
      return true;
    }
    await sleep(600);
  }
  console.warn('[Agent] Send verification failed - outgoing bubble not found');
  return false;
}

async function clickSendButton(expectedMessage) {
  // Polling loop — checks every 600 ms for up to 35 s.
  // Handles interstitials that appear at any point during WA Web SPA rendering,
  // not just within a fixed initial window.
  const TIMEOUT_MS = 35000;
  const POLL_MS    = 600;
  const started    = Date.now();
  let interstitialDone = false;

  while (Date.now() - started < TIMEOUT_MS) {

    // ── Priority 1: send button visible → click and return ─────────────────
    const sendEl = document.querySelector(SEND_SELECTORS.join(','));
    if (sendEl) {
      console.log('[Agent] Send click - button found');
      sendEl.click();
      await sleep(1000);
      // Enter-key fallback: if compose box still has text, React click didn't fire
      const box = document.querySelector(
        '[data-testid="conversation-compose-box-input"],' +
        'div[contenteditable="true"][data-tab],' +
        'div[contenteditable="true"][spellcheck]'
      );
      if (box && box.textContent.trim()) {
        console.log('[Agent] Send click fallback - compose still has text, pressing Enter');
        box.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Enter', code: 'Enter', keyCode: 13, which: 13,
          bubbles: true, cancelable: true,
        }));
        await sleep(500);
      }
      const verified = await _verifyOutgoingMessage(expectedMessage, 18000);
      if (!verified) {
        return {
          success: false,
          status: 'send_failed',
          error: 'Send click happened but outgoing message was not verified in WhatsApp DOM',
        };
      }
      return { success: true, status: 'sent', error: null };
    }

    // ── Priority 2: interstitial for unknown/new numbers → dismiss once ────
    if (!interstitialDone) {
      const contEl =
        document.querySelector(CONTINUE_SELECTORS.join(',')) ||
        findButtonByTextContains(
          'Continue', 'OK', 'Got it', 'Continue to chat',
          'Message', 'Open chat', 'Start chat', 'Chat'
        );
      if (contEl) {
        contEl.click();
        interstitialDone = true;
        await sleep(4000); // wait for WA to open the actual chat after interstitial
        // Newer WhatsApp Web auto-sends the ?text= pre-fill when the interstitial
        // button is clicked. Check for that before resuming the send-button loop.
        if (expectedMessage) {
          const alreadySent = await _verifyOutgoingMessage(expectedMessage, 3000);
          if (alreadySent) {
            console.log('[Agent] Interstitial auto-sent message verified — returning success');
            return { success: true, status: 'sent', error: null };
          }
        }
        continue;
      }
    }

    // ── Priority 3: close any catalog / greeting popup blocking compose ────
    const closeEl =
      document.querySelector(CLOSE_SELECTORS.join(',')) ||
      findButtonByText('Close', 'Dismiss');
    if (closeEl) {
      closeEl.click();
      await sleep(1000);
      continue;
    }

    await sleep(POLL_MS);
  }

  return {
    success: false,
    status: 'dom_not_loaded',
    error: 'Send button not found after 35 s — check that WhatsApp Web is logged in and the number is valid on WhatsApp.',
  };
}

// Find a button/role=button element by its visible text (case-insensitive exact match)
function findButtonByText(...texts) {
  const candidates = document.querySelectorAll('button, [role="button"], div[tabindex="0"]');
  for (const el of candidates) {
    const t = el.textContent.trim().toLowerCase();
    if (texts.some(text => t === text.toLowerCase())) return el;
  }
  return null;
}

// Find a button whose text STARTS WITH any of the given keywords (prefix match).
// Handles "Message +91 98765 43210" matching keyword "Message", etc.
function findButtonByTextContains(...keywords) {
  const candidates = document.querySelectorAll('button, [role="button"], div[tabindex="0"]');
  for (const el of candidates) {
    const t = el.textContent.trim().toLowerCase();
    if (keywords.some(kw => t === kw.toLowerCase() || t.startsWith(kw.toLowerCase()))) {
      return el;
    }
  }
  return null;
}

// ── Incoming-message scanner ───────────────────────────────────────────────────
// Scans the WhatsApp Web sidebar for unread conversations every 15 seconds.

// Extract the best available sender name from a chat row using multiple fallbacks.
// Critical: span[title] may be absent in GoLogin's WA build — each fallback targets
// a different DOM pattern seen across WhatsApp Web versions.
function _extractSenderName(row) {
  // 1. span[title] — most stable; title attr survives text truncation
  const spanTitle = row.querySelector('span[title]');
  if (spanTitle) {
    const t = (spanTitle.getAttribute('title') || spanTitle.textContent || '').trim();
    if (t && t.length <= 80) return _cleanChatLabel(t);
  }
  // 2. [data-testid="cell-frame-title"] text content
  const titleTestId = row.querySelector('[data-testid="cell-frame-title"]');
  if (titleTestId) {
    const t = titleTestId.textContent.trim();
    if (t && t.length <= 80) return _cleanChatLabel(t);
  }
  // 3. [dir="auto"][title] — RTL-aware name spans in some WA builds
  const dirTitle = row.querySelector('[dir="auto"][title]');
  if (dirTitle) {
    const t = (dirTitle.getAttribute('title') || dirTitle.textContent || '').trim();
    if (t && t.length <= 80) return _cleanChatLabel(t);
  }
  // 4. span[dir="auto"] text — WA sets dir="auto" on name spans for bidi support
  const dirSpan = row.querySelector('span[dir="auto"]');
  if (dirSpan) {
    const t = dirSpan.textContent.trim();
    if (t && t.length >= 2 && t.length <= 80) return _cleanChatLabel(t);
  }
  // 5. aria-label on the row itself — "Contact name, N unread messages" format
  const ariaLabel = row.getAttribute('aria-label') || '';
  if (ariaLabel) {
    const namePart = ariaLabel.split(',')[0].trim();
    if (namePart && namePart.length >= 2 && namePart.length <= 80
        && !/unread|message|notification/i.test(namePart)) {
      return _cleanChatLabel(namePart);
    }
  }
  // All 5 strategies failed — log which selectors were present for debugging
  console.log('[Scanner] Name extraction failed —',
    'span[title]=' + (row.querySelector('span[title]') ? 'YES' : 'no'),
    '| cell-frame-title=' + (row.querySelector('[data-testid="cell-frame-title"]') ? 'YES' : 'no'),
    '| dir-auto-title=' + (row.querySelector('[dir="auto"][title]') ? 'YES' : 'no'),
    '| span[dir=auto]=' + (row.querySelector('span[dir="auto"]') ? 'YES' : 'no'),
    '| aria="' + ariaLabel.slice(0, 60) + '"',
    '| html[:300]:', (row.innerHTML || '').slice(0, 300).replace(/\s+/g, ' '));
  return '';
}

function scanUnreadChats() {
  try {
    const results = [];
    const seen = new Set();

    const pane = document.querySelector('#pane-side')
              || document.querySelector('[aria-label="Chat list"]')
              || document.querySelector('[aria-label="Chats"]')
              || document.querySelector('[data-testid="chatlist"]');
    if (!pane) {
      console.log('[Scanner] No sidebar pane found — #pane-side absent');
      return [];
    }

    const _BADGE_SEL = '[data-testid="icon-unread-count"], [data-testid="unread-count"], [data-testid="badge-count"]';
    const _paneAllBadges = pane.querySelectorAll(_BADGE_SEL);
    if (_paneAllBadges.length > 0) {
      console.log('[Scanner] Unread badges in pane:', _paneAllBadges.length);
    }

    // Strategy 1: badge → closest row → name.
    // Badge elements ARE the ground truth for unread state.  Walking up from the
    // badge guarantees we only process rows that actually have a badge, regardless
    // of whether the badge is a descendant or a sibling of the name element.
    for (const badge of _paneAllBadges) {
      const count = parseInt((badge.textContent || '').trim(), 10) || 1;
      const row = badge.closest('[data-testid="cell-frame-container"]')
               || badge.closest('[role="listitem"]')
               || badge.closest('li');
      if (!row) {
        console.log('[Scanner] Badge has no parent row — badge:', (badge.outerHTML || '').slice(0, 150));
        continue;
      }
      const senderName = _extractSenderName(row);
      if (!senderName) continue; // _extractSenderName logs the failure above
      if (_isBadChatName(senderName)) {
        console.log('[Scanner] Bad name filtered:', senderName);
        continue;
      }
      const phone = _extractPhone(row);
      const jid   = _extractJid(row);
      const key   = jid || phone || senderName.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      results.push({
        sender_name:  senderName,
        sender_phone: phone,
        jid,
        preview:      _extractPreview(row, senderName),
        count,
        source:       'badge_direct',
      });
    }

    // Strategy 2: [role="listitem"] rows with per-row unread detection.
    // Catches builds where badge data-testid is absent but aria-label or numeric
    // span still signals unread state.
    for (const row of pane.querySelectorAll('[role="listitem"]')) {
      const unread = _hasUnreadInRow(row);
      if (!unread.found) continue;
      const senderName = _extractSenderName(row);
      if (!senderName || _isBadChatName(senderName)) continue;
      const phone = _extractPhone(row);
      const jid   = _extractJid(row);
      const key   = jid || phone || senderName.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      results.push({
        sender_name:  senderName,
        sender_phone: phone,
        jid,
        preview:      _extractPreview(row, senderName),
        count:        unread.count,
        source:       unread.source,
      });
    }

    // Strategy 3: aria-label "N unread" on row container (some WA builds)
    if (results.length === 0) {
      for (const row of pane.querySelectorAll('[data-testid="cell-frame-container"], [role="listitem"]')) {
        const label = row.getAttribute('aria-label') || '';
        const m = label.match(/(\d+)\s+unread/i);
        if (!m) continue;
        const senderName = _extractSenderName(row) || _cleanChatLabel(label.split(',')[0]);
        if (!senderName || _isBadChatName(senderName)) continue;
        const phone = _extractPhone(row);
        const jid   = _extractJid(row);
        const key   = jid || phone || senderName.toLowerCase();
        if (seen.has(key)) continue;
        seen.add(key);
        results.push({
          sender_name:  senderName,
          sender_phone: phone,
          jid,
          preview:      _extractPreview(row, senderName),
          count:        parseInt(m[1], 10) || 1,
          source:       'aria_label',
        });
      }
    }

    // Strategy 4: jid/phone-only — badge present but name completely unavailable.
    // Ensures messages are never silently dropped even when name extraction fails.
    if (results.length === 0) {
      for (const badge of pane.querySelectorAll(_BADGE_SEL)) {
        const row = badge.closest('[role="listitem"]') || badge.closest('li');
        if (!row) continue;
        const jid   = _extractJid(row);
        const phone = jid ? jid.split('@')[0] : _extractPhone(row);
        if (!jid && !phone) continue;
        const key = jid || phone;
        if (seen.has(key)) continue;
        seen.add(key);
        const count = parseInt((badge.textContent || '').trim(), 10) || 1;
        results.push({
          sender_name:  phone || jid,
          sender_phone: phone,
          jid,
          preview:      _extractPreview(row, ''),
          count,
          source:       'jid_only',
        });
      }
    }

    // Filter out JIDs that are suppressed because a send task is actively targeting them.
    // This prevents outgoing campaign sends from re-entering the auto-reply pipeline.
    const now = Date.now();
    const filtered = results.filter(r => {
      const rjid = r.jid || '';
      if (!rjid) return true;
      const exp = _suppressedJids.get(rjid);
      if (exp && now < exp) {
        console.log('[Scanner] Suppressed jid=%s sender=%s — filtered: active send target', rjid, r.sender_name || '');
        return false;
      }
      if (exp) _suppressedJids.delete(rjid);
      return true;
    });

    if (filtered.length > 0) {
      console.log('[Scanner] Resolved chats:', filtered.length, '—',
        filtered.map(r => r.sender_name + (r.sender_phone ? '(' + r.sender_phone + ')' : '') + ' src=' + r.source).join(' | '));
    }
    return filtered;
  } catch (e) {
    console.log('[Scanner] scanUnreadChats error:', e.message);
    _debug('sidebar_scan_error', { error: e.message });
    return [];
  }
}

function _cleanChatLabel(label) {
  return (label || '').replace(/\s+/g, ' ').replace(/^\d+\s+/, '').trim();
}

function _isBadChatName(name) {
  if (/^(archived|archive)$/i.test(name)) return true;
  if (/unread|message|notification/i.test(name)) return true;
  // Filter UI count labels like "5 \u0938\u0902\u0926\u0947\u0936" (digits + only script chars) but NOT
  // real Hindi contact names that legitimately contain digits (e.g. "Ramesh 9").
  if (/^\d+\s*[\u0900-\u097F]+$/.test(name)) return true;
  return false;
}

function _sidebarRows() {
  const pane = document.querySelector('#pane-side')
            || document.querySelector('[aria-label="Chat list"]')
            || document.querySelector('[aria-label="Chats"]')
            || document.querySelector('[data-testid="chatlist"]');
  if (!pane) return [];

  const rows = [];
  const seen = new Set();
  pane.querySelectorAll(
    '[data-testid="cell-frame-container"], [role="listitem"], [role="row"], div[tabindex="-1"], div[tabindex="0"]'
  ).forEach(row => {
    if (seen.has(row)) return;
    seen.add(row);
    const text = (row.textContent || '').trim();
    if (text.length < 2 || text.length > 1000) return;
    if (!row.querySelector('span')) return;
    rows.push(row);
  });
  return rows;
}

function _hasUnreadInRow(row) {
  // 1. data-testid badge — most reliable across WA Web versions
  const testidBadge = row.querySelector(
    '[data-testid="icon-unread-count"], [data-testid="unread-count"], [data-testid="badge-count"]'
  );
  if (testidBadge) {
    const n = parseInt((testidBadge.textContent || '').trim(), 10);
    return { found: true, count: n > 0 ? n : 1, source: 'testid' };
  }

  // 2. aria-label containing "unread" on row or any descendant
  const ariaEls = [row, ...row.querySelectorAll('[aria-label]')];
  for (const el of ariaEls) {
    const label = (el.getAttribute && el.getAttribute('aria-label')) || '';
    if (!/unread/i.test(label)) continue;
    const m = label.match(/(\d+)\s+unread/i) || label.match(/unread[^\d]*(\d+)/i);
    return { found: true, count: m ? (parseInt(m[1], 10) || 1) : 1, source: 'aria' };
  }

  // 3. Numeric badge span (1–999) — catches obfuscated testid/aria builds
  for (const sp of row.querySelectorAll('span')) {
    const t = (sp.textContent || '').trim();
    const n = parseInt(t, 10);
    if (!isNaN(n) && n > 0 && n <= 999 && String(n) === t) {
      return { found: true, count: n, source: 'numeric' };
    }
  }

  return { found: false, count: 0, source: '' };
}

// Extract the last-message preview text from a chat row.
// Tries known data-testid selectors first; falls back to a heuristic span search.
function _extractPreview(row, senderName) {
  const byTestId =
    row.querySelector('[data-testid="last-msg-text"]') ||
    row.querySelector('[data-testid="last-msg-status"] + span') ||
    row.querySelector('[data-testid="msg-meta"] + span') ||
    row.querySelector('[data-testid="conversation-info-header-chat-title"] + span');

  if (byTestId) {
    const t = byTestId.textContent.trim();
    if (t && t !== senderName) return t;
  }

  // Heuristic: scan spans for something that looks like a message
  for (const sp of row.querySelectorAll('span')) {
    const t = sp.textContent.trim();
    if (!t || t === senderName || t.length > 300 || t.length < 2) continue;
    if (/^\d{1,2}:\d{2}/.test(t) || /^\d+$/.test(t)) continue; // times or bare numbers
    if (/^(AM|PM|Yesterday|Today)$/i.test(t)) continue;
    // Strip "You: " prefix that WhatsApp adds for sent messages
    return t.replace(/^(You|you):\s*/, '');
  }
  return '';
}

// Parse a raw WhatsApp JID ("919876543210@c.us", "false_91..._HASH@c.us") → digits
function _parseJid(rawId) {
  const beforeAt = (rawId || '').split('@')[0];
  if (/^\d+$/.test(beforeAt)) return beforeAt;
  for (const part of beforeAt.split('_')) {
    if (/^\d{7,15}$/.test(part)) return part;
  }
  return '';
}

// Extract the full stable WhatsApp JID (e.g. "919812345678@c.us") from a chat row.
// This is the ROUTING IDENTITY used for all send decisions — never active_chat.
// Walks up ancestors looking for data-id containing "@c.us", then falls back to
// building a JID from the phone number when the attribute is absent.
function _extractJid(row) {
  // Walk up ancestors — chat rows in some WA builds have data-id on a parent
  let el = row;
  while (el && el !== document.body) {
    const id = el.getAttribute && el.getAttribute('data-id');
    if (id && /@(c\.us|g\.us)/.test(id)) {
      // Extract the canonical phone@c.us part, stripping message hash prefix
      const m = id.match(/((?:\d{7,20}|120363\d{5,})@(c\.us|g\.us))/);
      if (m) return m[1];
    }
    el = el.parentElement;
  }
  // Search descendants — some builds nest data-id inside the row
  const desc = row.querySelector('[data-id*="@c.us"], [data-id*="@g.us"]');
  if (desc) {
    const id = desc.getAttribute('data-id') || '';
    const m = id.match(/((?:\d{7,20}|120363\d{5,})@(c\.us|g\.us))/);
    if (m) return m[1];
  }
  // Build JID from phone if data-id is unavailable
  const phone = _extractPhone(row);
  return phone ? phone + '@c.us' : '';
}

// Extract phone from the data-id attribute on or above/below a chat row.
// The old approach used closest('[data-id]') which stops at the FIRST ancestor
// with any data-id — including wrappers whose data-id is not a phone JID.
// Now we walk every ancestor until we find one with "@c.us".
function _extractPhone(row) {
  // Walk upward through all ancestors
  let el = row;
  while (el && el !== document.body) {
    const id = el.getAttribute && el.getAttribute('data-id');
    if (id && id.includes('@c.us')) return _parseJid(id);
    el = el.parentElement;
  }
  // Also search descendants (some WA Web versions put data-id deeper)
  const desc = row.querySelector('[data-id*="@c.us"], [data-id*="@g.us"]');
  if (desc) return _parseJid(desc.getAttribute('data-id'));
  // Fallback: aria-label on the row sometimes contains phone for unsaved contacts
  // e.g. aria-label="Chat with +91 98765 43210, 2 unread messages"
  const label = row.getAttribute('aria-label') || '';
  const m = label.match(/\+?\d[\d\s\-\(\)]{8,}/);
  if (m) {
    const digits = m[0].replace(/\D/g, '');
    if (digits.length >= 7) return digits;
  }
  return '';
}

// Look up the phone number for a contact name by scanning the sidebar.
function _findPhoneByName(senderName) {
  const lower = senderName.toLowerCase();
  const rows = document.querySelectorAll(
    '[data-testid="cell-frame-container"], [role="listitem"]'
  );
  for (const row of rows) {
    const nameEl = row.querySelector('[data-testid="cell-frame-title"] span')
                || row.querySelector('[data-testid="cell-frame-title"]')
                || row.querySelector('span[title]');
    if (!nameEl) continue;
    const name = (nameEl.getAttribute('title') || nameEl.textContent.trim()).toLowerCase();
    if (name === lower) return _extractPhone(row);
  }
  return '';
}

// ── Chat-row helpers ──────────────────────────────────────────────────────────

function _nameMatches(a, b) {
  a = (a || '').toLowerCase().trim();
  b = (b || '').toLowerCase().trim();
  if (!a || !b) return false;
  return a === b || a.includes(b) || b.includes(a);
}

function _findChatRow(nameLower) {
  const rows = document.querySelectorAll('[role="listitem"], [data-testid="cell-frame-container"]');
  for (const row of rows) {
    const nameEl = row.querySelector('span[title]')
                || row.querySelector('[data-testid="cell-frame-title"] span');
    if (!nameEl) continue;
    const rowName = (nameEl.getAttribute('title') || nameEl.textContent.trim()).toLowerCase();
    if (_nameMatches(rowName, nameLower)) return row;
  }
  return null;
}

function _normalizeJid(value) {
  const raw = String(value || '').toLowerCase().trim();
  const match = raw.match(/(\d{7,20}|120363\d{5,})@(c\.us|g\.us)/);
  return match ? `${match[1]}@${match[2]}` : '';
}

// Extract JID from the current WhatsApp Web URL.
// After navigating to a chat (deep-link or SPA routing) the URL often contains
// the phone number or JID — this is the most reliable source for business chats
// whose sidebar rows lack a data-id attribute.
function _extractJidFromUrl() {
  const href = location.href || '';
  // Deep-link: /send?phone=919812345678  or  &phone=...
  const phoneMatch = href.match(/[?&]phone=(\d{7,20})/);
  if (phoneMatch) return phoneMatch[1] + '@c.us';
  // SPA hash / internal routing sometimes embeds the full JID
  const jidMatch = href.match(/((?:\d{7,20}|120363\d{5,})@(?:c\.us|g\.us))/);
  if (jidMatch) return jidMatch[1];
  return '';
}

// ── React Fiber / WA Internal Store Extraction ────────────────────────────────
// These functions extract stable chat identifiers from WhatsApp's internal
// React component tree and webpack module cache.  More reliable than DOM-only
// scraping because they survive sidebar DOM changes and WA Web updates.

/**
 * Walk the React fiber tree rooted at `element` and return the first
 * WhatsApp JID found in any component's memoized props.
 */
function _jidFromFiber(element) {
  if (!element) return '';
  try {
    const fiberKey = Object.keys(element).find(k =>
      k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance')
    );
    if (!fiberKey) return '';
    let fiber = element[fiberKey];
    let depth = 0;
    while (fiber && depth++ < 200) {
      try {
        const props = fiber.memoizedProps || fiber.pendingProps || {};
        // WA Chat model: props.chat.id._serialized / props.model.id._serialized
        const chatObj = props.chat || props.model || props.contact || props.conversation;
        if (chatObj && chatObj.id && chatObj.id._serialized) {
          const s = chatObj.id._serialized;
          if (/@(c\.us|g\.us)$/.test(s)) return s;
        }
        // Some components pass jid/wid directly
        const rawJid = props.jid || props.wid;
        if (rawJid) {
          const s = typeof rawJid === 'object' ? (rawJid._serialized || '') : String(rawJid);
          if (s && /@(c\.us|g\.us)$/.test(s)) return s;
        }
      } catch (_) {}
      fiber = fiber.return;
    }
  } catch (_) {}
  return '';
}

/**
 * Scan WhatsApp's webpack module cache for the active Chat model.
 * WA Web uses Webpack 5 (webpackChunkwhatsapp_web_client) since 2022.
 * Returns the Chat model object with .id._serialized, .name, .pushname, etc.
 */
function _getActiveChatFromWAStore() {
  try {
    const chunkArr = window.webpackChunkwhatsapp_web_client;
    if (!chunkArr || !Array.isArray(chunkArr)) return null;

    // Find the webpack require function from chunk runtime entries
    let requireFn = null;
    for (let i = chunkArr.length - 1; i >= 0; i--) {
      const chunk = chunkArr[i];
      if (chunk && typeof chunk[2] === 'function') { requireFn = chunk[2]; break; }
    }
    if (!requireFn || !requireFn.c) return null;

    const modCache = requireFn.c;
    for (const id in modCache) {
      try {
        const exp = modCache[id] && modCache[id].exports;
        if (!exp) continue;
        // Check module and common sub-exports for a store with getActive()
        for (const cand of [exp, exp.default, exp.Chat, exp.store, exp.Store]) {
          if (!cand || (typeof cand !== 'object' && typeof cand !== 'function')) continue;
          if (typeof cand.getActive === 'function') {
            const a = cand.getActive();
            if (a && a.id && a.id._serialized) return a;
          }
          if (cand.active && cand.active.id && cand.active.id._serialized) return cand.active;
        }
      } catch (_) {}
    }
  } catch (_) {}
  return null;
}

/**
 * Find the latest incoming message's data-id attribute in the open chat pane.
 * This is the stable WhatsApp message identifier (not DOM-generated).
 */
function _extractLatestMessageMeta() {
  const main = document.querySelector('#main');
  if (!main) return { msgId: '', datasetId: '' };
  const els = Array.from(main.querySelectorAll('[data-id]')).filter(el => {
    if (el.classList && el.classList.contains('message-out')) return false;
    if (el.closest && el.closest('.message-out')) return false;
    return true;
  });
  for (let i = els.length - 1; i >= 0; i--) {
    const id = els[i].getAttribute('data-id') || '';
    if (id) return { msgId: id, datasetId: els[i].id || id };
  }
  return { msgId: '', datasetId: '' };
}

/**
 * Extract comprehensive stable metadata from the currently open chat.
 * Tries strategies in order: URL → React fiber → WA webpack store → DOM data-id.
 * Returns { jid, serialized_id, phone, pushname, chat_type, message_id, dataset_id,
 *           extraction_method, hydrated }.
 */
function _extractOpenChatMetadata() {
  const meta = {
    jid: '', serialized_id: '', phone: '', pushname: '', chat_type: 'private',
    message_id: '', dataset_id: '', extraction_method: 'none', hydrated: false,
  };

  // 1. URL — most reliable after deep-link navigation
  const urlJid = _extractJidFromUrl();
  if (urlJid) {
    meta.jid = urlJid; meta.serialized_id = urlJid;
    meta.phone = urlJid.split('@')[0];
    meta.chat_type = urlJid.includes('@g.us') ? 'group' : 'private';
    meta.extraction_method = 'url'; meta.hydrated = true;
  }

  // 2. React fiber on #main header (works when URL has no JID after SPA nav)
  if (!meta.jid) {
    const header = document.querySelector('#main header');
    if (header) {
      const fj = _jidFromFiber(header);
      if (fj) {
        meta.jid = fj; meta.serialized_id = fj;
        meta.phone = fj.split('@')[0];
        meta.chat_type = fj.includes('@g.us') ? 'group' : 'private';
        meta.extraction_method = 'react_fiber'; meta.hydrated = true;
      }
    }
  }

  // 3. WA internal webpack store — has pushname, isBusiness, full chat model
  if (!meta.jid) {
    try {
      const chat = _getActiveChatFromWAStore();
      if (chat && chat.id && chat.id._serialized) {
        const s = chat.id._serialized;
        meta.jid = s; meta.serialized_id = s;
        meta.phone = chat.id.user || s.split('@')[0];
        meta.pushname = (chat.pushname || chat.notifyName || chat.name || '').trim();
        meta.chat_type = chat.id.server === 'g.us' ? 'group'
          : (chat.isBusiness ? 'business' : 'private');
        meta.extraction_method = 'react_store'; meta.hydrated = true;
      }
    } catch (_) {}
  }

  // 4. DOM data-id on message elements (fallback when store unavailable)
  if (!meta.jid) {
    const main = document.querySelector('#main');
    if (main) {
      const dj = _extractJid(main);
      if (dj) {
        meta.jid = dj; meta.serialized_id = dj;
        meta.phone = dj.split('@')[0];
        meta.chat_type = dj.includes('@g.us') ? 'group' : 'private';
        meta.extraction_method = 'dom_data_id'; meta.hydrated = true;
      }
    }
  }

  // Always extract message dataset ID (independent of which JID strategy succeeded)
  const mm = _extractLatestMessageMeta();
  meta.message_id = mm.msgId;
  meta.dataset_id = mm.datasetId;

  return meta;
}

// Wait until the conversation pane has loaded (header title OR a message with data-id).
// Returns true if the conversation loaded within timeoutMs, false if it timed out.
function _waitForConversationLoad(timeoutMs) {
  return new Promise(resolve => {
    const deadline = Date.now() + (timeoutMs || 5000);
    function check() {
      const main = document.querySelector('#main');
      if (main) {
        const hasHeader = main.querySelector(
          'header span[title], ' +
          'header [data-testid="conversation-info-header-chat-title"], ' +
          'header span[dir="auto"]'
        );
        const hasMsg = main.querySelector('[data-id*="@c.us"], [data-id*="@g.us"]');
        if (hasHeader || hasMsg) { resolve(true); return; }
      }
      if (Date.now() >= deadline) { resolve(false); return; }
      setTimeout(check, 350);
    }
    check();
  });
}

function _findChatRowByJid(jid) {
  jid = _normalizeJid(jid);
  if (!jid) return null;
  const rows = document.querySelectorAll('[role="listitem"], [data-testid="cell-frame-container"], li');
  for (const row of rows) {
    if (_extractJid(row) === jid) return row;
  }
  return null;
}

async function sendByJid(jid, message) {
  jid = _normalizeJid(jid);
  if (!jid) return { success: false, status: 'invalid_jid', error: 'Invalid JID' };

  for (let attempt = 1; attempt <= 3; attempt++) {
    console.log('[Agent] OpenChat attempt - jid="%s" attempt=%d', jid, attempt);
    const targetRow = _findChatRowByJid(jid);
    if (!targetRow) {
      if (attempt < 3) { await sleep(1500); continue; }
      return { success: false, status: 'open_chat_failed', error: `Chat row not found for JID: ${jid}` };
    }

    targetRow.click();
    await sleep(1800);
    console.log('[Agent] OpenChat loaded - jid="%s" attempt=%d', jid, attempt);

    const activeJid = _activeChatJid();
    if (activeJid && activeJid !== jid) {
      _debug('sendbyjid_chat_mismatch', { target: jid, active: activeJid, attempt });
      if (attempt < 3) { await sleep(1000); continue; }
      return { success: false, status: 'open_chat_failed', error: `Chat mismatch: target=${jid} active=${activeJid}` };
    }

    const composebox = await waitForAny([
      '[data-testid="conversation-compose-box-input"]',
      'div[contenteditable="true"][data-tab]',
      'div[contenteditable="true"][spellcheck="true"]',
    ], 8000);
    if (!composebox) {
      console.warn('[InputBoxMissing] jid=%s attempt=%d — compose box not found', jid, attempt);
      if (attempt < 3) { await sleep(1000); continue; }
      return { success: false, status: 'dom_not_loaded', error: `Compose box not found for JID: ${jid}` };
    }

    console.log('[Agent] Message typing - jid="%s" chars=%d', jid, String(message || '').length);
    composebox.focus();
    document.execCommand('selectAll', false, null);
    document.execCommand('delete', false, null);
    document.execCommand('insertText', false, message);
    await sleep(500);

    const sendBtn = await waitForAny(SEND_SELECTORS, 5000);
    if (sendBtn) {
      console.log('[Agent] SendMessage click - jid="%s"', jid);
      sendBtn.click();
    } else {
      console.log('[Agent] SendMessage enter fallback - jid="%s"', jid);
      composebox.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true,
      }));
    }
    const verified = await _verifyOutgoingMessage(message, 18000);
    if (!verified) {
      return { success: false, status: 'send_failed', error: `Outgoing message not verified for JID ${jid}` };
    }
    console.log('[Agent] SendMessage verified - jid="%s"', jid);
    return { success: true, status: 'sent', error: null };
  }

  return { success: false, status: 'send_failed', error: `Failed to send to JID ${jid} after 3 attempts` };
}

// ── WhatsApp readiness + scanner initialisation ───────────────────────────────
// WhatsApp Web is a React SPA; the chat list renders asynchronously after the
// page shell loads. Starting the scan interval immediately (document_idle) means
// the first several ticks find nothing. _waitForWhatsApp polls for the sidebar
// panel and only resolves once it is actually in the DOM.

function _waitForWhatsApp(maxWait) {
  return new Promise(resolve => {
    const deadline = Date.now() + (maxWait || 60000);
    function check() {
      if (
        document.querySelector('#pane-side') ||
        document.querySelector('[data-testid="cell-frame-container"]') ||
        document.querySelector('[aria-label="Chat list"]') ||
        document.querySelector('[aria-label="Chats"]') ||
        document.querySelector('[data-testid="chatlist"]')
      ) {
        resolve(true);
        return;
      }
      if (Date.now() >= deadline) { resolve(false); return; }
      setTimeout(check, 1000);
    }
    check();
  });
}

function _sendToBackground(messages) {
  console.log('[Scanner] WS-Send payload — count:', messages.length,
    '| senders:', messages.map(m => m.sender_name + (m.sender_phone ? '(' + m.sender_phone + ')' : '') + ' src=' + (m.source || '?')).join(', '));
  _debug('content_forward_incoming', { count: messages.length, source: messages[0]?.source || 'sidebar' });
  chrome.runtime.sendMessage({ type: 'INCOMING_MESSAGES', messages }, () => {
    const err = chrome.runtime.lastError;
    if (err) console.warn('[Scanner] INCOMING_MESSAGES send error:', err.message);
    else console.log('[Scanner] INCOMING_MESSAGES delivered to background SW');
  });
}

function _debug(message, data) {
  chrome.runtime.sendMessage({ type: 'AGENT_DEBUG', message, data }, () => {
    void chrome.runtime.lastError;
  });
}

// _activeChatName() is used only for scanner/debug visibility.
// It is NOT used as a routing identifier; all routing goes through whatsapp_jid.
function _activeChatName() {
  const el = document.querySelector('#main header span[title]')
          || document.querySelector('#main header [data-testid="conversation-info-header-chat-title"]')
          || document.querySelector('#main header [role="button"] span[title]')
          || document.querySelector('#main header [role="button"] span[dir="auto"]')
          || document.querySelector('#main header span[dir="auto"]')
          || document.querySelector('header span[title]');
  return _cleanChatLabel(el ? (el.getAttribute('title') || el.textContent || '') : '');
}

function _activeChatJid() {
  // 1. URL is the most reliable source after deep-link or SPA navigation
  const urlJid = _extractJidFromUrl();
  if (urlJid) return urlJid;

  // 2. Scan #main descendants — message data-id attributes contain the JID
  //    (only available once at least one message has rendered)
  const main = document.querySelector('#main');
  if (!main) return '';
  return _extractJid(main);
}

// _initActiveChatObserver() and _activeIncomingMessages() have been removed.
// They read messages from the currently open chat (active_chat dependency) and
// caused replies to be routed to whatever chat the user last opened, not the
// actual sender.  Sidebar scanning via _initSidebarObserver() + _extractJid()
// is the sole incoming detection path.

let _lastSidebarDebugAt = 0;
let _badgeRetryTimer = null;

// JIDs suppressed from incoming processing because a send task is actively targeting them.
// background.js sends SUPPRESS_INCOMING_JID before opening a chat; expiry is timestamp ms.
const _suppressedJids = new Map(); // jid → expiry_ms

// ── Active conversation reconciliation cache ──────────────────────────────────
// Prevents re-forwarding the same message every 20 s while it sits in an open chat.
const _processedMsgCache = new Map(); // hashKey → timestamp

function _pruneProcessedCache() {
  if (_processedMsgCache.size > 200) {
    const toDelete = Array.from(_processedMsgCache.keys()).slice(0, 50);
    toDelete.forEach(k => _processedMsgCache.delete(k));
  }
}

function _isPlaceholderPreview(preview) {
  const text = (preview || '').trim().toLowerCase();
  if (!text) return true;
  if (text === 'default-contact-refreshed') return true;
  if (text === 'wa-chat-psa') return true;
  if (/^\d+\s+unread messages?$/.test(text)) return true;
  if (/^unread messages?$/.test(text)) return true;
  return false;
}

function _latestIncomingTextFromActiveChat() {
  const main = document.querySelector('#main');
  if (!main) return '';

  const incoming = Array.from(main.querySelectorAll(
    '.message-in, [data-testid="msg-container"]'
  )).filter(el => {
    if (el.classList && el.classList.contains('message-out')) return false;
    if (el.closest && el.closest('.message-out')) return false;
    return true;
  });

  for (let i = incoming.length - 1; i >= 0; i--) {
    const el = incoming[i];
    const textEl = el.querySelector('.selectable-text, span.selectable-text, [data-pre-plain-text]');
    const raw = (textEl ? textEl.innerText || textEl.textContent : el.innerText || el.textContent || '').trim();
    const lines = raw.split('\n').map(x => x.trim()).filter(Boolean);
    const text = lines.find(x =>
      x.length > 1 &&
      x.length < 1000 &&
      !/^\d{1,2}:\d{2}\s*(am|pm)?$/i.test(x) &&
      !/^(read|delivered|sent)$/i.test(x)
    );
    if (text && !_isPlaceholderPreview(text)) return text;
  }
  return '';
}

async function _openUnreadChatAndExtract(msg) {
  const row = (msg.jid && _findChatRowByJid(msg.jid))
           || _findChatRow((msg.sender_name || '').toLowerCase());
  if (!row) {
    _debug('unread_open_skipped', { reason: 'row_not_found', jid: msg.jid || '', sender: msg.sender_name || '' });
    return msg;
  }

  console.log('[OpenChat] attempt - jid=%s sender=%s', msg.jid || '', msg.sender_name || '');
  _debug('unread_open_attempt', { jid: msg.jid || '', sender: msg.sender_name || '', source: msg.source || '' });
  row.click();

  // Retry up to 3 times, waiting for the conversation pane to render each time.
  for (let attempt = 1; attempt <= 3; attempt++) {
    const loaded = await _waitForConversationLoad(attempt === 1 ? 5000 : 3000);
    const activeName = _activeChatName();
    const incoming   = _latestIncomingTextFromActiveChat();

    // Use multi-strategy extraction: URL → React fiber → WA store → DOM data-id
    const chatMeta = _extractOpenChatMetadata();
    const activeJid = chatMeta.jid;

    console.log('[ChatLoaded] attempt=%d loaded=%s jid=%s method=%s name=%s text=%s',
                attempt, loaded, activeJid || '', chatMeta.extraction_method,
                activeName || '', (incoming || '').slice(0, 40));

    if (activeJid && incoming) {
      // Full success: JID + incoming text both extracted
      console.log('[JIDExtracted] jid=%s method=%s sender=%s',
                  activeJid, chatMeta.extraction_method, msg.sender_name || activeName || '');
      _debug('unread_extract_success', {
        jid: activeJid,
        method: chatMeta.extraction_method,
        sender: activeName || msg.sender_name,
        preview: incoming.slice(0, 80),
      });
      return {
        ...msg,
        sender_name: msg.sender_name || activeName,
        sender_phone: msg.sender_phone || chatMeta.phone || '',
        jid: activeJid,
        preview: incoming,
        source: (msg.source || 'sidebar') + '_opened',
        // Stable identifier fields
        serialized_id: chatMeta.serialized_id || activeJid,
        pushname: chatMeta.pushname || '',
        chat_type: chatMeta.chat_type || 'private',
        message_id: chatMeta.message_id || '',
        dataset_id: chatMeta.dataset_id || '',
        extraction_method: chatMeta.extraction_method,
        hydrated: chatMeta.hydrated,
        chat_opened: true,
      };
    }

    if (attempt < 3) continue;

    // Final attempt — partial success: JID found but no incoming text
    // (media message, template, or empty conversation). Keep sidebar preview.
    if (activeJid) {
      console.log('[JIDExtracted] partial - jid=%s method=%s sender=%s no_text',
                  activeJid, chatMeta.extraction_method, msg.sender_name || activeName || '');
      _debug('unread_extract_partial', {
        reason: 'no_incoming_text',
        jid: activeJid,
        method: chatMeta.extraction_method,
        sender: activeName || msg.sender_name,
      });
      return {
        ...msg,
        sender_name: msg.sender_name || activeName,
        sender_phone: msg.sender_phone || chatMeta.phone || '',
        jid: activeJid,
        preview: msg.preview || '',
        source: (msg.source || 'sidebar') + '_opened_partial',
        serialized_id: chatMeta.serialized_id || activeJid,
        pushname: chatMeta.pushname || '',
        chat_type: chatMeta.chat_type || 'private',
        message_id: chatMeta.message_id || '',
        dataset_id: chatMeta.dataset_id || '',
        extraction_method: chatMeta.extraction_method,
        hydrated: chatMeta.hydrated,
        chat_opened: true,
      };
    }

    // Complete failure: could not determine JID after 3 attempts
    const reason = !loaded ? 'chat_load_timeout' : 'jid_missing';
    console.log('[OpenChat] failed - reason=%s sender=%s active=%s', reason, msg.sender_name || '', activeName || '');
    _debug('unread_extract_empty', {
      reason,
      jid: msg.jid || '',
      sender: msg.sender_name || '',
      active_chat: activeName || '',
    });
    return msg;
  }

  return msg;
}

async function _enrichUnreadMessages(messages) {
  const enriched = [];
  for (const msg of messages) {
    if (_isPlaceholderPreview(msg.preview) || !msg.jid) {
      enriched.push(await _openUnreadChatAndExtract(msg));
    } else {
      enriched.push(msg);
    }
  }
  // Keep messages that have a validated JID even with no extractable text
  // (media messages, business templates).  Messages without a JID still require
  // a non-placeholder preview so the backend can generate a contextual reply.
  return enriched.filter(msg => {
    const jid = _normalizeJid(msg.jid || '');
    if (jid) return true;                        // valid JID — always forward
    return !_isPlaceholderPreview(msg.preview);  // no JID — need preview
  });
}

async function _scanVirtualizedUnreadChats(reason) {
  const pane = document.querySelector('#pane-side')
            || document.querySelector('[aria-label="Chat list"]')
            || document.querySelector('[aria-label="Chats"]')
            || document.querySelector('[data-testid="chatlist"]');
  if (!pane) return [];

  const originalTop = pane.scrollTop;
  const seen = new Map();
  const maxSteps = 20;
  const stepSize = Math.max(250, Math.floor(pane.clientHeight * 0.85));

  for (let step = 0; step < maxSteps; step++) {
    for (const msg of scanUnreadChats()) {
      const key = msg.jid || msg.sender_phone || msg.sender_name;
      if (key && !seen.has(key)) seen.set(key, msg);
    }
    const before = pane.scrollTop;
    pane.scrollTop = Math.min(pane.scrollTop + stepSize, pane.scrollHeight);
    await sleep(250);
    if (pane.scrollTop === before || pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 4) break;
  }

  pane.scrollTop = originalTop;
  _debug('sidebar_virtualized_traversal_complete', {
    reason,
    found: seen.size,
    scroll_height: pane.scrollHeight,
  });
  return Array.from(seen.values());
}

async function _scanSidebarAndForward(reason) {
  let messages = scanUnreadChats();
  if (messages.length === 0 && reason !== 'mutation') {
    const fallback = await _scanVirtualizedUnreadChats(reason);
    if (fallback.length > 0) {
      console.log('[FallbackScanRecovered] reason=%s found=%d — virtualized scroll recovered missed chats',
                  reason, fallback.length);
    }
    messages = fallback;
  }
  const rows = _sidebarRows();
  const now = Date.now();
  const badgeEls = document.querySelectorAll(
    '[data-testid="icon-unread-count"], [data-testid="unread-count"], [data-testid="badge-count"]'
  ).length;

  // Always log to console so DevTools shows every scan tick
  console.log('[UnreadScan] reason=%s badges=%d rows=%d resolved=%d', reason, badgeEls, rows.length, messages.length);
  console.log('[Scanner] scan(' + reason + ') badges=' + badgeEls + ' rows=' + rows.length + ' resolved=' + messages.length);

  if (badgeEls > 0 && messages.length === 0) {
    console.log('[UnreadMissing] badges=%d reason=%s — badges visible but no chats resolved', badgeEls, reason);
    console.log('[Scanner] WARN: badges visible but no chats resolved — check name extraction above');
  }

  if (messages.length > 0 || now - _lastSidebarDebugAt > 30000) {
    _lastSidebarDebugAt = now;
    _debug('sidebar_scan_complete', {
      reason,
      rows: rows.length,
      count: messages.length,
      badge_els: badgeEls,
      url: location.href,
      active_chat: _activeChatName(),
    });
  }

  if (messages.length > 0) {
    messages = await _enrichUnreadMessages(messages);
  }

  if (messages.length > 0) {
    console.log('[UnreadRecovered] count=%d reason=%s senders=%s',
                messages.length, reason,
                messages.map(m => m.sender_name || m.jid || '?').join(','));
    _sendToBackground(messages);
    clearTimeout(_badgeRetryTimer);
    _badgeRetryTimer = null;
    return;
  }

  // Badges visible but no chats mapped → DOM may not have settled yet.
  // Retry once after 2 s with a distinct reason so we don't recurse infinitely.
  if (badgeEls > 0 && reason !== 'badge_retry' && !_badgeRetryTimer) {
    _badgeRetryTimer = setTimeout(() => {
      _badgeRetryTimer = null;
      _scanSidebarAndForward('badge_retry');
    }, 2000);
  }
}

// Returns metadata about the latest INCOMING message in the currently open chat.
// Always re-queries the DOM to avoid stale refs from React rerenders / GoLogin restores.
function _getLatestIncomingWithMeta() {
  const main = document.querySelector('#main');
  if (!main) return null;

  const msgs = Array.from(main.querySelectorAll(
    '.message-in, [data-testid="msg-container"]'
  )).filter(el => {
    if (el.classList && el.classList.contains('message-out')) return false;
    if (el.closest && el.closest('.message-out')) return false;
    return true;
  });

  if (!msgs.length) return null;

  const last = msgs[msgs.length - 1];
  const idEl = last.getAttribute('data-id')
    ? last
    : (last.closest ? last.closest('[data-id]') : null);
  const msgId = idEl ? (idEl.getAttribute('data-id') || '') : '';

  const textEl = last.querySelector('.selectable-text, span.selectable-text, [data-pre-plain-text]');
  const raw = (textEl ? textEl.innerText || textEl.textContent : last.innerText || last.textContent || '').trim();
  const lines = raw.split('\n').map(x => x.trim()).filter(Boolean);
  const text = lines.find(x =>
    x.length > 1 &&
    x.length < 1000 &&
    !/^\d{1,2}:\d{2}\s*(am|pm)?$/i.test(x) &&
    !/^(read|delivered|sent)$/i.test(x)
  ) || '';

  return { text, msgId, hasText: !!text };
}

// Poll the currently active/open conversation for unprocessed incoming messages.
// Runs every 20 s as a fallback for chats that were already open when the observer
// attached, or where the unread badge never fired (GoLogin restored tab, etc.).
async function _activeConversationPoll() {
  if (!_isWhatsAppReady()) return;

  const jid = _activeChatJid();
  if (!jid || !_normalizeJid(jid)) return;

  // Skip JIDs that background.js marked as active send targets to prevent
  // outgoing campaign opens from re-entering the incoming pipeline.
  const _suppExp = _suppressedJids.get(jid);
  if (_suppExp) {
    if (Date.now() < _suppExp) {
      console.log('[ActivePollSuppressed] jid=%s — recently sent to, skipping active poll', jid);
      return;
    }
    _suppressedJids.delete(jid);
  }

  const name = _activeChatName();
  const meta = _getLatestIncomingWithMeta();

  console.log('[ActiveConversationScan] jid=%s name=%s', jid, name || '');

  if (!meta) return;
  if (!meta.hasText && !meta.msgId) return; // no extractable content

  // Stable dedup key: prefer immutable message data-id; fall back to jid+text snippet
  const hashKey = jid + ':' + (meta.msgId || meta.text.slice(0, 60));
  if (_processedMsgCache.has(hashKey)) return;

  console.log('[ActiveConversationDetected] jid=%s name=%s', jid, name || '');
  console.log('[UnprocessedIncomingFound] jid=%s text=%s', jid, (meta.text || '').slice(0, 60));

  _processedMsgCache.set(hashKey, Date.now());
  _pruneProcessedCache();

  const msg = {
    jid,
    sender_name: name || '',
    sender_phone: jid.replace(/@c\.us$/, '').replace(/@g\.us$/, ''),
    preview: meta.text || '',
    source: 'active_conversation_poll',
    count: 1,
  };

  // Enrich with stable identifiers from the currently open chat
  const chatMeta = _extractOpenChatMetadata();
  msg.serialized_id  = chatMeta.serialized_id || jid;
  msg.pushname       = chatMeta.pushname || '';
  msg.chat_type      = chatMeta.chat_type || 'private';
  msg.message_id     = chatMeta.message_id || meta.msgId || '';
  msg.dataset_id     = chatMeta.dataset_id || '';
  msg.extraction_method = chatMeta.extraction_method || 'active_poll';
  msg.hydrated       = chatMeta.hydrated;
  msg.chat_opened    = false;

  console.log('[ReconciliationQueue] jid=%s method=%s source=active_conversation_poll',
              jid, msg.extraction_method);
  _sendToBackground([msg]);
}

function _initSidebarObserver() {
  const pane = document.querySelector('#pane-side')
            || document.querySelector('[aria-label="Chat list"]')
            || document.querySelector('[aria-label="Chats"]')
            || document.querySelector('[data-testid="chatlist"]');
  if (!pane) {
    console.log('[Scanner] Cannot attach sidebar observer — pane element not found');
    _debug('sidebar_observer_missing_pane', { url: location.href });
    return;
  }

  console.log('[Scanner] MutationObserver attached to', pane.id || pane.getAttribute('aria-label') || pane.tagName);
  _debug('sidebar_observer_started', { rows: _sidebarRows().length });
  let timer = null;
  const observer = new MutationObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(() => _scanSidebarAndForward('mutation'), 400);
  });
  observer.observe(pane, { childList: true, subtree: true, characterData: true });
}

async function _initScanner() {
  console.log('[Scanner] Initialising — waiting for WhatsApp Web to load...');
  const ready = await _waitForWhatsApp(120000); // wait up to 2 min for WA to load
  if (!ready) {
    console.log('[Scanner] WhatsApp Web not ready after 2 min — scanner aborted');
    _debug('whatsapp_not_ready_for_scanner', {});
    return;
  }

  console.log('[Scanner] WhatsApp Web ready — attaching sidebar observer + interval');
  _debug('content_scanner_ready', { url: location.href });
  // Sidebar observer is the SOLE incoming detection path.
  // The active-chat observer (_initActiveChatObserver) is intentionally NOT called here:
  // it reads bubbles from the currently open chat (manual dependency) and causes
  // replies to be routed to whatever chat the user last opened, not the actual sender.
  _initSidebarObserver();

  // First scan immediately after WA is ready
  await sleep(1500);
  _scanSidebarAndForward('initial');

  // Periodic scan every 5 seconds — backup for mutations the observer may miss
  // (e.g. GoLogin DOM quirks, virtual-scroll updates, WA reconnects).
  setInterval(() => {
    _scanSidebarAndForward('interval');
  }, 5000);

  // Active conversation reconciliation — catches messages in already-open chats
  // where no unread badge fires (GoLogin restored session, manually opened chat,
  // WhatsApp DOM rerender cleared the badge before observer saw it).
  setTimeout(_activeConversationPoll, 3000);
  setInterval(_activeConversationPoll, 20000);
}

_initScanner();

// ── Helpers ────────────────────────────────────────────────────────────────────

function waitForAny(selectors, timeout) {
  return new Promise(resolve => {
    const joined = selectors.join(',');
    const found = document.querySelector(joined);
    if (found) { resolve(found); return; }

    const observer = new MutationObserver(() => {
      const el = document.querySelector(joined);
      if (el) {
        observer.disconnect();
        clearTimeout(timer);
        resolve(el);
      }
    });

    const timer = setTimeout(() => {
      observer.disconnect();
      resolve(null);
    }, timeout);

    observer.observe(document.body, { childList: true, subtree: true });
  });
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}
