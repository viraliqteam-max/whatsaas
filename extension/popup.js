document.addEventListener('DOMContentLoaded', async () => {
  const statusEl  = document.getElementById('status');
  const pidDisplay = document.getElementById('pid-display');
  const hintText  = document.getElementById('hint-text');

  const data = await chrome.storage.local.get(['profile_id', 'ws_status', 'server_ws_base']);
  applyStatus(data.ws_status || 'disconnected');
  applyProfileId(data.profile_id || null, data.server_ws_base || null);

  chrome.storage.onChanged.addListener((changes) => {
    if (changes.ws_status)      applyStatus(changes.ws_status.newValue);
    if (changes.profile_id || changes.server_ws_base) {
      chrome.storage.local.get(['profile_id', 'server_ws_base'], (d) => {
        applyProfileId(d.profile_id || null, d.server_ws_base || null);
      });
    }
  });

  function applyStatus(s) {
    statusEl.className = 'pill ' + (
      s === 'connected'  ? 'connected'  :
      s === 'connecting' ? 'connecting' :
      s === 'reconnecting' ? 'connecting' :
      'disconnected'
    );
    statusEl.textContent =
      s === 'connected'  ? '● Connected to server' :
      s === 'connecting' ? '◌ Connecting…'         :
      s === 'reconnecting' ? '◌ Reconnecting…'     :
                           '○ Disconnected';
  }

  function applyProfileId(pid, wsBase) {
    if (pid) {
      pidDisplay.textContent = pid;
      const serverUrl = wsBase
        ? wsBase.replace(/^ws/, 'http').replace('/ws/agent/', '')
        : 'unknown';
      hintText.innerHTML = `Profile ID was set automatically.<br>Server: <b>${serverUrl}</b>`;
    } else {
      pidDisplay.textContent = '—';
      hintText.innerHTML = 'Waiting for auto-configuration…<br>Launch this profile from GoLogin to connect automatically.';
    }
  }
});
