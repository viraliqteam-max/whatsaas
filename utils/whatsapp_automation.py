"""
WhatsApp Web automation helpers.
All functions accept a Selenium WebDriver that is already attached
to a GoLogin (or plain Chrome) profile.
"""
import base64
import logging
import time
import re
from io import BytesIO
from typing import Optional
from urllib.parse import quote

from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

logger = logging.getLogger(__name__)

WHATSAPP_WEB_URL = "https://web.whatsapp.com"

# CSS selectors — these match WhatsApp Web as of 2024-2025.
# If WhatsApp updates its DOM they may need adjustment.
SEL_QR_CANVAS = 'canvas[aria-label="Scan this QR code to link a device"]'
SEL_QR_CODE = '[data-ref]'              # fallback for older builds
SEL_MAIN_SIDE = "#side"                 # left panel — visible when logged in
SEL_SEARCH_BOX = '[data-testid="chat-list-search"]'
SEL_MSG_INPUT = '[data-testid="conversation-compose-box-input"]'
SEL_SEND_BTN = '[data-testid="compose-btn-send"]'
SEL_CONTACT_TITLE = '[data-testid="conversation-info-header-chat-title"]'
SEL_LOADING_SCREEN = '[data-testid="intro-text"]'


# ---------------------------------------------------------------------------
# Session status
# ---------------------------------------------------------------------------

class WhatsAppStatus:
    LOADING = "loading"
    QR_REQUIRED = "qr_required"
    LOGGED_IN = "logged_in"
    ERROR = "error"


def get_status(driver: WebDriver, navigate: bool = True) -> dict:
    """
    Navigate to WhatsApp Web and return session status + QR code if needed.
    Returns:
        {
            "status": "loading" | "qr_required" | "logged_in" | "error",
            "qr_code_base64": "..." | None,
            "message": str
        }
    """
    try:
        if navigate:
            driver.get(WHATSAPP_WEB_URL)
            time.sleep(4)

        # Already logged in?
        try:
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, SEL_MAIN_SIDE))
            )
            return {"status": WhatsAppStatus.LOGGED_IN, "qr_code_base64": None, "message": "Session active"}
        except TimeoutException:
            pass

        # QR code present?
        try:
            qr_element = WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, SEL_QR_CANVAS))
            )
            qr_b64 = _screenshot_element_base64(driver, qr_element)
            return {
                "status": WhatsAppStatus.QR_REQUIRED,
                "qr_code_base64": qr_b64,
                "message": "Scan the QR code with WhatsApp on your phone",
            }
        except TimeoutException:
            pass

        # Still loading
        return {"status": WhatsAppStatus.LOADING, "qr_code_base64": None, "message": "WhatsApp Web is loading"}

    except WebDriverException as exc:
        logger.error("get_status error: %s", exc)
        return {"status": WhatsAppStatus.ERROR, "qr_code_base64": None, "message": str(exc)}


def wait_for_login(driver: WebDriver, timeout: int = 60) -> bool:
    """Block until the user scans the QR code or timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, SEL_MAIN_SIDE))
            )
            return True
        except TimeoutException:
            time.sleep(2)
    return False


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

def send_message(driver: WebDriver, phone_number: str, message: str) -> dict:
    """
    Send a WhatsApp message to a phone number (international format, no +).
    Uses the wa.me deep-link approach which works even for unsaved contacts.
    Returns {"success": bool, "error": str | None}
    """
    try:
        encoded_msg = quote(message)
        url = f"{WHATSAPP_WEB_URL}/send?phone={phone_number}&text={encoded_msg}"
        driver.get(url)
        time.sleep(4)

        # Wait for the send button to appear
        send_btn = WebDriverWait(driver, 20).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, SEL_SEND_BTN))
        )
        send_btn.click()
        time.sleep(2)

        logger.info("Message sent to %s", phone_number)
        return {"success": True, "error": None}

    except TimeoutException:
        error = f"Timeout waiting for send button (number: {phone_number})"
        logger.error(error)
        return {"success": False, "error": error}
    except WebDriverException as exc:
        error = str(exc)
        logger.error("send_message error: %s", error)
        return {"success": False, "error": error}


def send_message_to_saved_contact(driver: WebDriver, contact_name: str, message: str) -> dict:
    """
    Search for a saved contact by name and send a message.
    """
    try:
        # Ensure we're on WhatsApp Web
        if WHATSAPP_WEB_URL not in driver.current_url:
            driver.get(WHATSAPP_WEB_URL)
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, SEL_MAIN_SIDE))
            )

        # Click search box
        search = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, SEL_SEARCH_BOX))
        )
        search.click()
        search.send_keys(contact_name)
        time.sleep(2)

        # Click first result
        first_result = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, '[data-testid="cell-frame-container"]'))
        )
        first_result.click()
        time.sleep(1)

        # Type and send message
        msg_box = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, SEL_MSG_INPUT))
        )
        msg_box.click()
        msg_box.send_keys(message)
        msg_box.send_keys(Keys.RETURN)
        time.sleep(2)

        return {"success": True, "error": None}

    except (TimeoutException, NoSuchElementException) as exc:
        error = str(exc)
        logger.error("send_message_to_saved_contact error: %s", error)
        return {"success": False, "error": error}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _screenshot_element_base64(driver: WebDriver, element) -> str:
    """Take a screenshot of a specific element and return as base64 PNG."""
    try:
        png = element.screenshot_as_png
        return base64.b64encode(png).decode("utf-8")
    except Exception:
        # Fallback: full-page screenshot
        png = driver.get_screenshot_as_png()
        return base64.b64encode(png).decode("utf-8")


def take_screenshot(driver: WebDriver) -> str:
    """Return a full-page screenshot as base64 PNG."""
    return base64.b64encode(driver.get_screenshot_as_png()).decode("utf-8")


# ---------------------------------------------------------------------------
# Inbox polling — reads unread message previews from the sidebar
# ---------------------------------------------------------------------------

# Selectors for chat list items. WhatsApp may update these; adjust if broken.
SEL_CHAT_ITEM       = '[data-testid="cell-frame-container"]'
SEL_CHAT_TITLE      = '[data-testid="cell-frame-title"] span'
SEL_UNREAD_BADGE    = '[data-testid="icon-unread-count"]'
SEL_MSG_PREVIEW     = '[data-testid="last-msg-status"] ~ span, span.x1iyjqo2'


def poll_inbox(driver: WebDriver) -> list[dict]:
    """
    Scan the WhatsApp Web chat sidebar for chats that have unread messages.

    Does NOT click into any chat, so it is safe to call while a campaign
    is actively sending — the current browser URL is not changed.

    Returns a list of dicts:
        [{"sender_name": str, "message_preview": str, "unread_count": int}, ...]

    Raises WebDriverException if Chrome has crashed (caller should handle this).
    """
    # If not on WhatsApp Web, navigate there and wait for sidebar
    if WHATSAPP_WEB_URL not in driver.current_url:
        driver.get(WHATSAPP_WEB_URL)
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, SEL_MAIN_SIDE))
            )
        except TimeoutException:
            logger.warning("poll_inbox: WhatsApp Web sidebar did not load (not logged in?)")
            return []

    unread_chats = []

    try:
        chat_rows = driver.find_elements(By.CSS_SELECTOR, SEL_CHAT_ITEM)
    except WebDriverException:
        raise  # Chrome is dead — let the caller handle it

    for row in chat_rows:
        try:
            # Skip chats with no unread badge
            badges = row.find_elements(By.CSS_SELECTOR, SEL_UNREAD_BADGE)
            if not badges:
                continue

            # Extract the unread count from the badge text
            unread_count = 1
            for badge in badges:
                try:
                    unread_count = int(badge.text.strip()) if badge.text.strip() else 1
                except ValueError:
                    unread_count = 1

            # Extract sender name (shown in the sidebar title)
            sender_name = ""
            title_els = row.find_elements(By.CSS_SELECTOR, SEL_CHAT_TITLE)
            if title_els:
                sender_name = title_els[0].text.strip()

            # Extract message preview text
            message_preview = ""
            # Try to get the message preview span next to the status icon
            preview_els = row.find_elements(
                By.CSS_SELECTOR,
                'span.x1iyjqo2, [data-testid="last-msg-status"] span'
            )
            if preview_els:
                message_preview = preview_els[0].text.strip()

            outer_html = row.get_attribute("outerHTML") or ""
            jid_match = re.search(r"((?:\d{7,20}|120363\d{5,})@(c\.us|g\.us))", outer_html)
            jid = jid_match.group(1) if jid_match else ""
            phone = jid.split("@", 1)[0] if jid.endswith("@c.us") else ""

            if not jid and not phone:
                logger.debug("poll_inbox: skipping unread row without jid sender=%s", sender_name)
                continue

            unread_chats.append({
                "sender_name":    sender_name,
                "sender_phone":   phone,
                "jid":            jid,
                "message_preview": message_preview,
                "unread_count":   unread_count,
            })

        except Exception:
            # Skip this row if anything goes wrong reading it
            continue

    logger.info("poll_inbox: found %d unread chat(s)", len(unread_chats))
    return unread_chats
