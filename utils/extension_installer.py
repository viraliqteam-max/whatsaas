"""
Automatic Chrome extension installer for AdsPower profiles.

AdsPower stores each profile's Chrome user-data directory at:
  Windows: %LOCALAPPDATA%\\adspower_global\\cwd_global\\{user_id}\\

We copy the extension folder into that profile's Extensions directory and
write an entry into the profile's Preferences so Chrome loads it without
needing the user to click "Load Unpacked".

This runs once per profile on first launch.
"""
import json
import logging
import os
import shutil
from pathlib import Path

from django.conf import settings

logger = logging.getLogger(__name__)

# Path to our built extension (sibling of manage.py)
EXTENSION_SRC = Path(settings.BASE_DIR) / "extension"

# Stable internal extension ID derived from the folder name (not signed, so
# Chrome assigns a random ID on first load — we write a fixed key instead).
# For unpacked extensions Chrome uses the hash of the extension's public key.
# Since we ship without a key.pem, Chrome assigns a random ID each install.
# We store the resulting ID in a marker file so we only install once.
MARKER_FILENAME = ".wa_agent_installed"

# AdsPower default profile root on Windows
_ADS_ROOT_WIN = Path(os.environ.get("LOCALAPPDATA", "")) / "adspower_global" / "cwd_global"


def _ads_profile_dir(profile_id: str) -> Path | None:
    """Return the Chrome user-data directory for an AdsPower profile, or None."""
    if not _ADS_ROOT_WIN.exists():
        return None
    candidate = _ADS_ROOT_WIN / profile_id
    return candidate if candidate.exists() else None


def is_installed(profile_id: str) -> bool:
    """Return True if we have already installed the extension for this profile."""
    profile_dir = _ads_profile_dir(profile_id)
    if not profile_dir:
        return False
    return (profile_dir / MARKER_FILENAME).exists()


def install_extension(profile_id: str) -> bool:
    """
    Copy the extension into the AdsPower profile directory and register it
    in the Chrome Preferences file so it loads automatically.

    Returns True on success, False if the profile directory was not found
    (e.g. AdsPower is installed in a non-standard location).
    """
    if not EXTENSION_SRC.exists():
        logger.warning("Extension source not found at %s — skipping install", EXTENSION_SRC)
        return False

    profile_dir = _ads_profile_dir(profile_id)
    if not profile_dir:
        logger.warning(
            "AdsPower profile directory for %s not found at %s — "
            "load the extension manually via AdsPower settings.",
            profile_id, _ADS_ROOT_WIN
        )
        return False

    # Target: {profile_dir}/Default/Extensions/wa_automation_agent/1.0.0/
    ext_dest = profile_dir / "Default" / "Extensions" / "wa_automation_agent" / "1.0.0"
    ext_dest.mkdir(parents=True, exist_ok=True)

    # Copy all extension files
    for item in EXTENSION_SRC.iterdir():
        dst = ext_dest / item.name
        if item.is_file():
            shutil.copy2(item, dst)

    # Patch Chrome Preferences to register the extension as enabled
    prefs_path = profile_dir / "Default" / "Preferences"
    _patch_preferences(prefs_path, ext_dest)

    # Write marker so we don't reinstall every launch
    (profile_dir / MARKER_FILENAME).write_text("1")

    logger.info("Extension installed for AdsPower profile %s at %s", profile_id, ext_dest)
    return True


def _patch_preferences(prefs_path: Path, ext_path: Path) -> None:
    """Add the extension entry to Chrome's Preferences JSON."""
    if not prefs_path.exists():
        logger.warning("Preferences file not found at %s — Chrome may auto-create it on first run", prefs_path)
        return

    try:
        with open(prefs_path, "r", encoding="utf-8") as f:
            prefs = json.load(f)
    except Exception as exc:
        logger.warning("Could not read Preferences: %s", exc)
        return

    ext_id = "waautomationagent0000000000000000"  # 32-char placeholder ID

    prefs.setdefault("extensions", {}).setdefault("settings", {})[ext_id] = {
        "active_permissions": {"api": ["tabs", "storage", "scripting", "notifications"], "manifest_permissions": []},
        "app_launch_ordinal": "n",
        "creation_flags": 1,
        "from_bookmark": False,
        "from_webstore": False,
        "granted_permissions": {"api": ["tabs", "storage", "scripting", "notifications"], "manifest_permissions": []},
        "location": 4,
        "manifest": {
            "description": "WhatsApp Automation Agent",
            "manifest_version": 3,
            "name": "WhatsApp Automation Agent",
            "version": "1.0.0",
        },
        "path": str(ext_path),
        "state": 1,
        "was_installed_by_default": False,
        "was_installed_by_oem": False,
    }

    try:
        with open(prefs_path, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except Exception as exc:
        logger.warning("Could not write Preferences: %s", exc)
