// Content script — injected into http://localhost:8000/ext-init/* at document_start.
// Runs reliably even when the service worker hasn't started yet, because Chrome's
// renderer injects content scripts independently of the SW lifecycle.
// Sends the profile_id to the background SW (which wakes it up if sleeping).

(function () {
  const match = location.pathname.match(/\/ext-init\/([^/?#]+)\/?/);
  if (!match) return;
  const pid = match[1];
  chrome.runtime.sendMessage({ type: 'SET_PROFILE_ID', profile_id: pid });
})();
