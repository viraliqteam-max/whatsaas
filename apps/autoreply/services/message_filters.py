import re


SYSTEM_NAMES = (
    "archived",
    "archive",
    "unread",
    "notification",
    "whatsapp business",
)

SYSTEM_PREVIEWS = (
    "default-contact-refreshed",
    "default-group-refreshed",
    "archive-refreshed",
    "wa-chat-psa",
    "1 unread message",
    "unread message",
)


def clean_sender_name(sender_name: str) -> str:
    sender_name = (sender_name or "").strip()
    digits = re.sub(r"\D", "", sender_name)
    if len(digits) >= 7 and re.match(r"^[\+\d\s\-\(\)]+$", sender_name):
        return digits
    return sender_name


def should_ignore_incoming_event(sender_name: str, preview: str) -> bool:
    lower_name = (sender_name or "").strip().lower()
    lower_preview = (preview or "").strip().lower()

    if not lower_name or len(sender_name or "") > 80:
        return True
    if any(token in lower_name for token in SYSTEM_NAMES):
        return True
    if lower_preview in SYSTEM_PREVIEWS:
        return True
    if lower_preview.endswith(" unread message") or lower_preview.endswith(" unread messages"):
        return True
    # Only filter pure count labels like "5 \u0938\u0902\u0926\u0947\u0936" (digit(s) + only Devanagari).
    # Do NOT filter real Hindi names that contain digits, e.g. "Ramesh 9", "Class 10".
    if re.match(r"^\d+\s*[\u0900-\u097F]+$", sender_name or ""):
        return True
    return False
