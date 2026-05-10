/**
 * Offscreen document — lives alongside the service worker indefinitely.
 *
 * Its only job: hold an open chrome.runtime port so Chrome never marks the
 * service worker as "idle" and kills it.  Without this, Chrome MV3 terminates
 * the SW after ~30 seconds when no WhatsApp Web tab is open yet.
 */

let _port = null;

function connect() {
  try {
    _port = chrome.runtime.connect({ name: 'keepalive' });
    _port.onDisconnect.addListener(() => {
      // SW was restarted — reconnect the port so it stays alive again
      setTimeout(connect, 500);
    });
    setInterval(() => {
      try { _port.postMessage({ type: 'heartbeat' }); } catch (_) {}
    }, 20000);
  } catch (_) {
    setTimeout(connect, 3000);
  }
}

connect();
