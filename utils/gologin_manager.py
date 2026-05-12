"""
GoLogin Cloud profile manager — wraps the GoLogin REST API and maintains
an in-process pool of active Selenium WebDriver instances.

GoLogin desktop app must be running locally so profiles can be started.
Set GOLOGIN_API_TOKEN in .env (get it from app.gologin.com → Settings → API).
API base: https://api.gologin.com
"""
import logging
import os
import re
import threading
import time
from typing import Optional

from utils import wa_watcher

import requests

# Force SSL verification off for every requests.Session.send call in this process.
# The GoLogin Python SDK calls https://api.gologin.co internally and fails with
# CERTIFICATE_VERIFY_FAILED on Windows because the root CA is not trusted.
# Our own _api() already passes verify=False; this patch covers the SDK's calls.
_orig_session_send = requests.Session.send
def _unverified_send(self, request, **kwargs):
    kwargs['verify'] = False
    return _orig_session_send(self, request, **kwargs)
requests.Session.send = _unverified_send
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

from django.conf import settings

# Path to ChromeDriver matching GoLogin's Orbita Browser (Chrome 146).
# Downloaded to drivers/chromedriver.exe via dl_chromedriver.py.
_CHROMEDRIVER_PATH = os.path.join(settings.BASE_DIR, "drivers", "chromedriver.exe")

logger = logging.getLogger(__name__)

_GL_API_BASE = "https://api.gologin.com"

# In-process driver pool: { profile_id: {"driver": ..., "debug_addr": ...} }
_driver_pool: dict[str, dict] = {}
_pool_lock    = threading.Lock()

# GoLogin SDK instances kept alive so we can call gl.stop() on teardown
_gl_instances: dict[str, object] = {}


def _find_orbita_exe() -> str:
    """Locate the GoLogin Orbita browser exe — picks the highest installed version."""
    import glob as _glob_mod
    pattern = os.path.join(os.path.expanduser("~"), ".gologin", "browser", "orbita-browser-*", "chrome.exe")
    found = sorted(_glob_mod.glob(pattern))
    return found[-1] if found else ""


def _wait_for_debug_port(debug_addr: str, timeout: int = 30) -> None:
    """
    Poll until GoLogin's Orbita debug port accepts a TCP connection.
    Replaces the old fixed time.sleep(8): returns as soon as the port is
    open (typically 3-5 s) instead of always waiting the full 8 s.
    """
    import socket as _socket
    host, _, port_str = debug_addr.rpartition(":")
    port = int(port_str)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with _socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError(f"GoLogin debug port {debug_addr} not ready after {timeout}s")


# ---------------------------------------------------------------------------
# GoLogin REST API helpers
# ---------------------------------------------------------------------------

def _token() -> str:
    return getattr(settings, "GOLOGIN_API_TOKEN", "").strip()


def _headers() -> dict:
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def _api(method: str, path: str, **kwargs) -> dict:
    """Call the GoLogin Cloud API and return parsed JSON body."""
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    url = f"{_GL_API_BASE}{path}"
    resp = requests.request(method, url, headers=_headers(), timeout=30, verify=False, **kwargs)
    resp.raise_for_status()
    if resp.content:
        return resp.json()
    return {}


def list_gologin_profiles(limit: int = 50, page: int = 1) -> dict:
    """
    List GoLogin profiles.  Returns a shape compatible with the rest of the
    codebase: { "profiles": [{"id": ..., "name": ..., "os": ...}, ...] }.
    """
    data = _api("GET", f"/browser/v2?page={page}&perPage={limit}")
    raw = data.get("profiles", [])
    profiles = [
        {
            "id": p.get("id", ""),
            "name": p.get("name", ""),
            "os": p.get("os", "win"),
        }
        for p in raw
    ]
    return {"profiles": profiles}


def get_gologin_profile(profile_id: str) -> dict:
    return _api("GET", f"/browser/{profile_id}")


def create_gologin_profile(name: str, os_type: str = "win", extra: dict = None) -> dict:
    """
    Create a new GoLogin browser profile via the quick-create endpoint.
    After creation, sets the profile's startUrl so the Chrome extension can
    auto-configure its profile ID when the browser first opens.
    Returns {"id": "<profile_id>", "name": "<name>"}.
    """
    payload = {"name": name, "os": os_type}
    if extra:
        payload.update(extra)
    data = _api("POST", "/browser/quick", json=payload)
    profile_id   = data.get("id")
    profile_name = data.get("name", name)

    # Set startup URL — fetch-then-merge so we don't overwrite the full profile body
    if profile_id:
        backend_url = getattr(settings, "BACKEND_URL", "http://localhost:8000").rstrip("/")
        start_url   = f"{backend_url}/ext-init/{profile_id}/"
        try:
            current = _api("GET", f"/browser/{profile_id}")
            current["startUrl"] = start_url
            _api("PUT", f"/browser/{profile_id}", json=current)
            logger.info("Set startUrl for profile %s -> %s", profile_id, start_url)
        except Exception as exc:
            logger.warning("Could not set startUrl for profile %s: %s", profile_id, exc)

    return {"id": profile_id, "name": profile_name}


def update_gologin_profile(profile_id: str, payload: dict) -> dict:
    # GoLogin PUT requires the full profile — fetch first, then merge and write back
    current = _api("GET", f"/browser/{profile_id}")
    current.update(payload)
    return _api("PUT", f"/browser/{profile_id}", json=current)


def delete_gologin_profile(profile_id: str) -> dict:
    return _api("DELETE", f"/browser/{profile_id}")


# ---------------------------------------------------------------------------
# Profile start / stop — GoLogin Python SDK
# ---------------------------------------------------------------------------

def start_remote_profile(profile_id: str) -> str:
    """
    Launch the GoLogin profile browser using the official GoLogin Python SDK.

    The SDK downloads the profile fingerprint from GoLogin cloud, then launches
    the Orbita browser directly — no desktop app REST API required.
    Returns the debugger address (host:port) for ChromeDriver to attach.
    """
    from gologin import GoLogin

    # Remove any old copies of our extension that were manually uploaded to
    # GoLogin cloud (stored as userChromeExtensions). Those copies live in
    # ~/.gologin/extensions/user-extensions/ and get loaded into the browser
    # alongside our --load-extension copy, creating multiple extension instances
    # with different IDs and separate storages — causing the popup to show
    # "Profile ID: —" because the user clicks the wrong instance.
    # We always load our extension via extra_params --load-extension instead.
    from urllib.parse import urlparse as _urlparse
    backend_url  = getattr(settings, "BACKEND_URL", "http://localhost:8000").rstrip("/")
    backend_host = _urlparse(backend_url).hostname or "127.0.0.1"
    start_url    = f"{backend_url}/ext-init/{profile_id}/"

    try:
        profile_data = _api("GET", f"/browser/{profile_id}")
        needs_update = (
            bool(profile_data.get("userChromeExtensions")) or
            profile_data.get("startUrl") != start_url
        )
        # Disable any external proxy before launch.
        # External proxies (including GoLogin managed proxies) route ALL browser
        # traffic — including WebSocket — through a remote server that cannot reach
        # private LAN IPs (192.168.x.x / 10.x.x.x).  Adding bypass entries to the
        # GoLogin profile is unreliable because GoLogin configures the proxy via its
        # own Chrome extension (chrome.proxy API), which overrides the --proxy-bypass-list
        # CLI flag entirely.  Disabling the proxy is the only guaranteed fix.
        proxy = profile_data.get("proxy") or {}
        if proxy.get("mode") not in ("none", "direct", None, ""):
            proxy["mode"] = "none"
            profile_data["proxy"] = proxy
            needs_update = True
        if needs_update:
            profile_data["userChromeExtensions"] = []
            profile_data["startUrl"] = start_url
            _api("PUT", f"/browser/{profile_id}", json=profile_data)
            logger.info("Updated profile %s: cleared extensions, startUrl -> %s", profile_id, start_url)
    except Exception as exc:
        logger.warning("Could not update profile %s before launch: %s", profile_id, exc)

    extension_path = os.path.join(settings.BASE_DIR, "extension")
    orbita = _find_orbita_exe()

    options = {
        "token":        _token(),
        "profile_id":   profile_id,
        # Force-load our WhatsApp automation extension into the Orbita browser.
        # GoLogin only loads extensions registered in the cloud profile's
        # chromeExtensions list; extra_params bypasses that restriction.
        "extra_params": [
            f"--load-extension={extension_path}",
            # Chrome-level bypass: skip the external proxy for our backend host.
            # Complements the profile-level proxy.bypass field set above.
            f"--proxy-bypass-list={backend_host};127.0.0.1;localhost",
        ],
    }
    if orbita:
        # SDK option key is snake_case "executable_path", not camelCase
        options["executable_path"] = orbita

    gl = GoLogin(options)
    try:
        debug_addr = gl.start()
    except Exception as exc:
        exc_str = str(exc).lower()
        if "proxy" not in exc_str and "check failed" not in exc_str:
            raise
        # GoLogin proxy check failed (dead proxy server) — disable the proxy on the
        # profile and retry.  This also fixes the extension WebSocket issue since
        # external proxies can't reach private LAN IPs (192.168.x.x).
        logger.warning("GoLogin proxy check failed for %s (%s) — disabling proxy and retrying", profile_id, exc)
        try:
            pd = _api("GET", f"/browser/{profile_id}")
            if (pd.get("proxy") or {}).get("mode") not in ("none", "direct", None, ""):
                pd["proxy"]["mode"] = "none"
                _api("PUT", f"/browser/{profile_id}", json=pd)
                logger.info("Proxy disabled for profile %s", profile_id)
            gl = GoLogin(options)
            debug_addr = gl.start()
        except Exception as exc2:
            raise RuntimeError(f"GoLogin SDK launch failed for {profile_id} even after disabling proxy: {exc2}") from exc2

    if not debug_addr:
        raise RuntimeError(f"GoLogin SDK did not return a debugger address for profile {profile_id}")

    with _pool_lock:
        _gl_instances[profile_id] = gl

    logger.info("GoLogin SDK launched profile %s at %s", profile_id, debug_addr)
    return debug_addr


def stop_remote_profile(profile_id: str) -> dict:
    try:
        with _pool_lock:
            gl = _gl_instances.pop(profile_id, None)
        if gl:
            gl.stop()
            logger.info("GoLogin SDK stopped profile %s", profile_id)
    except Exception as exc:
        logger.warning("stop_remote_profile error (ignored): %s", exc)
    return {}


# ---------------------------------------------------------------------------
# Selenium driver management
# ---------------------------------------------------------------------------

def _build_driver_from_ws(debug_addr: str) -> webdriver.Chrome:
    """Attach a ChromeDriver instance to the already-running GoLogin Chrome."""
    chrome_options = Options()
    chrome_options.add_experimental_option("debuggerAddress", debug_addr)
    service = Service(executable_path=_CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.implicitly_wait(10)
    return driver


def _build_local_driver() -> webdriver.Chrome:
    """Fallback: launch a plain headless Chrome (no GoLogin needed)."""
    from webdriver_manager.chrome import ChromeDriverManager
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


def launch_profile(profile_id: str, use_remote: bool = True, on_disconnect=None) -> webdriver.Chrome:
    """
    Open the GoLogin profile and return a connected WebDriver.
    Reuses the existing driver if it is still alive (checked via driver.title).

    on_disconnect: optional callable(profile_id) forwarded to wa_watcher so the
    runtime lock is automatically released when the browser window closes.
    """
    with _pool_lock:
        if profile_id in _driver_pool:
            entry = _driver_pool[profile_id]
            driver = entry["driver"]
            try:
                _ = driver.title  # liveness probe
                logger.info("Reusing existing driver for profile %s", profile_id)
                _trigger_ext_init(driver, profile_id)
                # Restart the watcher if it crashed since the last launch.
                if entry.get("debug_addr"):
                    wa_watcher.start(profile_id, entry["debug_addr"], on_disconnect=on_disconnect)
                return driver
            except Exception:
                logger.warning("Stale driver for profile %s — relaunching", profile_id)
                _driver_pool.pop(profile_id, None)

    if use_remote and _token():
        debug_addr = start_remote_profile(profile_id)
        _wait_for_debug_port(debug_addr)        # returns as soon as port is open
        time.sleep(2)                           # let extension service worker finish init
        driver = _build_driver_from_ws(debug_addr)
        pid = _get_chrome_pid(debug_addr)
        with _pool_lock:
            _driver_pool[profile_id] = {"driver": driver, "debug_addr": debug_addr, "pid": pid}
        _trigger_ext_init(driver, profile_id)
        wa_watcher.start(profile_id, debug_addr, on_disconnect=on_disconnect)
    else:
        driver = _build_local_driver()
        with _pool_lock:
            _driver_pool[profile_id] = {"driver": driver, "debug_addr": None, "pid": None}

    logger.info("Driver launched for profile %s", profile_id)
    return driver


def _trigger_ext_init(driver: webdriver.Chrome, profile_id: str) -> None:
    """
    Fire the ext-init URL so the extension picks up the profile ID and connects.

    Uses execute_script (fire-and-forget) instead of driver.get() because
    background.js immediately redirects the tab to WhatsApp as soon as it sees
    the ext-init URL.  That redirect interrupts the page load and causes
    driver.get() to block for the full 10-second timeout — twice — wasting 24 s
    on every launch.  execute_script starts the navigation and returns instantly;
    ChromeDriver never waits for a page-load it cannot observe.
    """
    backend_url  = getattr(settings, "BACKEND_URL", "http://localhost:8000").rstrip("/")
    ext_init_url = f"{backend_url}/ext-init/{profile_id}/"
    try:
        driver.execute_script("window.location.href = arguments[0]", ext_init_url)
        logger.info("Triggered ext-init for profile %s", profile_id)
        time.sleep(3)  # let extension SW process the URL and open the WebSocket
    except Exception as exc:
        logger.debug("ext-init trigger error (non-fatal): %s", exc)


def stop_profile(profile_id: str) -> None:
    """Disconnect ChromeDriver and tell GoLogin to close the browser."""
    with _pool_lock:
        entry = _driver_pool.pop(profile_id, None)

    if entry:
        try:
            entry["driver"].quit()
        except Exception as exc:
            logger.warning("Driver quit error: %s", exc)

    wa_watcher.stop(profile_id)
    stop_remote_profile(profile_id)
    logger.info("Profile %s stopped", profile_id)


def get_active_driver(profile_id: str) -> Optional[webdriver.Chrome]:
    with _pool_lock:
        entry = _driver_pool.get(profile_id)
    return entry["driver"] if entry else None


def get_debug_addr(profile_id: str) -> Optional[str]:
    """Return the CDP debug address for an active profile, or None."""
    with _pool_lock:
        entry = _driver_pool.get(profile_id)
    return entry["debug_addr"] if entry else None


def get_browser_pid(profile_id: str) -> Optional[int]:
    """Return the Chrome process PID for an active profile, or None."""
    with _pool_lock:
        entry = _driver_pool.get(profile_id)
    if not entry:
        return None
    if entry.get("pid"):
        return entry["pid"]
    # Lazy PID resolution from debug_addr if not stored yet
    debug_addr = entry.get("debug_addr")
    if debug_addr:
        pid = _get_chrome_pid(debug_addr)
        if pid:
            entry["pid"] = pid
        return pid
    return None


def list_active_profiles() -> list[str]:
    with _pool_lock:
        return list(_driver_pool.keys())


def _get_chrome_pid(debug_addr: str) -> Optional[int]:
    """Find the Chrome process PID by matching its remote-debugging-port."""
    try:
        port = int(debug_addr.split(":")[-1])
        import psutil
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                if any(f"--remote-debugging-port={port}" in arg for arg in cmdline):
                    return proc.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception as exc:
        logger.debug("_get_chrome_pid failed for %s: %s", debug_addr, exc)
    return None
