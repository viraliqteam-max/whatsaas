import re


JID_RE = re.compile(r"^(?P<id>\d{7,20}|120363\d{5,})@(?P<domain>c\.us|g\.us)$")


def normalize_jid(value: str = "", phone: str = "") -> str:
    raw = (value or "").strip().lower()
    raw = raw.replace("mailto:", "").strip("<>[]() ")

    match = re.search(r"(\d{7,20}|120363\d{5,})@(c\.us|g\.us)", raw)
    if match:
        return f"{match.group(1)}@{match.group(2)}"

    digits = re.sub(r"\D", "", phone or raw)
    if len(digits) >= 7:
        return f"{digits}@c.us"
    return ""


def validate_jid(jid: str) -> bool:
    return bool(JID_RE.match(normalize_jid(jid)))


def jid_to_phone(jid: str) -> str:
    jid = normalize_jid(jid)
    if not jid or jid.endswith("@g.us"):
        return ""
    value = jid.split("@", 1)[0]
    return value if re.match(r"^\d{7,20}$", value) else ""


def jid_from_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return f"{digits}@c.us" if len(digits) >= 7 else ""


def routing_key(profile_id: str, jid: str) -> str:
    return f"conversation:{profile_id}:{normalize_jid(jid)}"


def processing_lock_key(profile_id: str, jid: str) -> str:
    return f"processing_lock:{profile_id}:{normalize_jid(jid)}"
