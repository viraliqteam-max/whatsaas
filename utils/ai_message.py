"""
AI message personalization and context-aware auto-reply.

Priority order for auto-reply:
  1. Google Gemini free tier (1500 req/day — recommended, get key at aistudio.google.com)
  2. OpenAI (paid)
  3. Keyword rule-based (free, always works, human-like tone)

For outbound personalization:
  1. Gemini  2. OpenAI  3. Plain {name} substitution
"""
import logging
import re
import time
from threading import Lock
from django.conf import settings

logger = logging.getLogger(__name__)

_AI_TIMEOUT = 8.0

# ── Gemini rate limiter + 429 cooldown ───────────────────────────────────────
_gemini_call_times: list = []
_gemini_lock = Lock()
_GEMINI_RPM_CAP = 3          # keep Gemini light; Groq handles overflow
_gemini_cooldown_until: float = 0.0   # monotonic time when 429 cooldown expires
_GEMINI_429_COOLDOWN_SECONDS = 15 * 60

def _gemini_allowed() -> bool:
    """Return True (and record the call) if under RPM cap and not in 429 cooldown."""
    now = time.monotonic()
    with _gemini_lock:
        if now < _gemini_cooldown_until:
            return False
        _gemini_call_times[:] = [t for t in _gemini_call_times if now - t < 60]
        if len(_gemini_call_times) >= _GEMINI_RPM_CAP:
            return False
        _gemini_call_times.append(now)
        return True

# ── Keyword rules ──────────────────────────────────────────────────────────────
# Each rule: (list of trigger words/phrases, reply template)
# {name} in reply is replaced with sender_name if available.
# Rules are matched case-insensitively. First match wins.

_RULES = [
    # Greetings
    (["hi", "hello", "hey", "hii", "helo", "howdy", "good morning",
      "good evening", "good afternoon", "good night", "namaste", "namaskar"],
     "Hi {name}! 👋 Great to hear from you. How can I help you today?"),

    # Price / cost enquiry
    (["price", "cost", "how much", "rate", "charges", "fee", "pricing",
      "budget", "affordable", "expensive", "cheap", "quote", "quotation",
      "package", "plan", "plans"],
     "Thanks for asking, {name}! 😊 Our pricing is designed to be flexible based on your needs. "
     "Could you share a bit more about what you're looking for? I'll get you the right details right away."),

    # Demo / trial request
    (["demo", "trial", "free trial", "try", "test", "sample", "show me",
      "how does it work", "walkthrough", "tour"],
     "Absolutely, {name}! We'd love to show you a demo. 🎯 "
     "Just let me know a convenient time and I'll set it up for you!"),

    # Interest / want to know more
    (["interested", "want to know", "tell me more", "more info", "more information",
      "details", "learn more", "know more", "share more"],
     "Awesome, {name}! 🙌 I'm happy to share more. "
     "Could you tell me a bit about what you're specifically looking for?"),

    # Positive confirmation
    (["yes", "yeah", "yep", "sure", "okay", "ok", "sounds good", "perfect",
      "great", "proceed", "go ahead", "let's do it", "confirm"],
     "Great, {name}! 🎉 Let's move forward. I'll get everything sorted for you shortly!"),

    # Negative / not interested
    (["no", "nope", "not interested", "don't contact", "stop", "remove",
      "unsubscribe", "leave me alone", "go away"],
     "Completely understood, {name}. No worries at all! "
     "I'll make sure not to disturb you. Have a wonderful day! 😊"),

    # Support / help request
    (["help", "support", "issue", "problem", "not working", "error",
      "trouble", "stuck", "assist", "assistance"],
     "I'm here to help, {name}! 🛠️ "
     "Please describe your issue and I'll get it sorted as soon as possible."),

    # Location / office
    (["where", "location", "address", "office", "located", "find you",
      "visit", "branch", "store"],
     "Happy to help with that, {name}! 📍 "
     "Could you let me know which city you're in? I'll share the nearest details."),

    # Timing / availability
    (["when", "time", "hours", "timing", "schedule", "available",
      "open", "closed", "working hours", "office hours"],
     "Good question, {name}! ⏰ "
     "We're available Monday to Saturday, 9 AM – 6 PM. "
     "Is there a specific time you'd like to connect?"),

    # Payment / invoice
    (["payment", "pay", "invoice", "bill", "receipt", "transaction",
      "refund", "money", "bank"],
     "Sure, {name}! 💳 For payment-related queries, our team will assist you directly. "
     "Could you share more details about your concern?"),

    # Thank you
    (["thanks", "thank you", "thx", "ty", "appreciate", "grateful", "cheers"],
     "You're very welcome, {name}! 😊 "
     "If there's anything else I can help with, feel free to ask anytime!"),

    # Who are you / company
    (["who are you", "which company", "what company", "about you",
      "your name", "what is this", "what do you do", "what service"],
     "Hi {name}! I'm an automated assistant here to help you. "
     "A team member will follow up with you very soon with more details. 😊"),

    # Call / phone request
    (["call", "phone", "ring", "speak", "talk", "contact number",
      "callback", "call back", "whatsapp call"],
     "Of course, {name}! 📞 "
     "Please share a good time for a call and our team will reach out to you promptly."),
]


def _display_name(sender_name: str) -> str:
    """Return a human-friendly first name. Falls back to 'there' for phone-number names."""
    first = sender_name.split()[0] if sender_name else ""
    # If the first word is a phone prefix (+91, +1) or all digits/symbols, it's not a real name
    if not first or re.match(r'^[\+\d\(\)\-]+$', first):
        return "there"
    return first


def _keyword_reply(message: str, sender_name: str = "") -> str:
    """Match message against keyword rules and return a human-like reply."""
    lower = message.lower()
    display_name = _display_name(sender_name)
    for keywords, template in _RULES:
        if any(kw in lower for kw in keywords):
            return template.replace("{name}", display_name)
    return ""


# ── AI clients ────────────────────────────────────────────────────────────────

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent?key={key}"
)
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def _safe_error(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code:
        return f"{exc.__class__.__name__} status={status_code}"
    return exc.__class__.__name__


def _call_gemini(prompt: str) -> str:
    """Call Gemini REST API directly. SSL verify disabled for Python 3.14 Windows compat."""
    global _gemini_cooldown_until
    key = getattr(settings, "GEMINI_API_KEY", "").strip()
    if not key:
        return ""
    import requests, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    url = _GEMINI_URL.format(key=key)
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    resp = requests.post(url, json=payload, verify=False, timeout=_AI_TIMEOUT)
    if resp.status_code == 429:
        # Rate-limited: block Gemini long enough to stop repeated 429s.
        with _gemini_lock:
            _gemini_cooldown_until = time.monotonic() + _GEMINI_429_COOLDOWN_SECONDS
        logger.warning(
            "Gemini 429 - entering %s s cooldown, falling back to Groq/OpenAI/rules",
            _GEMINI_429_COOLDOWN_SECONDS,
        )
        resp.raise_for_status()
    resp.raise_for_status()
    return resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()


def _get_openai():
    key = getattr(settings, "OPENAI_API_KEY", "").strip()
    if not key:
        return None
    try:
        from openai import OpenAI
        return OpenAI(api_key=key, timeout=_AI_TIMEOUT)
    except ImportError:
        return None


def _call_groq(prompt: str, max_tokens: int) -> str:
    key = getattr(settings, "GROQ_API_KEY", "").strip()
    if not key:
        return ""

    import requests, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    model = getattr(settings, "GROQ_MODEL", "llama-3.1-8b-instant").strip()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.6,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        _GROQ_URL,
        json=payload,
        headers=headers,
        verify=False,
        timeout=_AI_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _plain(template_body: str, contact_name: str, contact_phone: str) -> str:
    return (
        template_body
        .replace("{name}", contact_name)
        .replace("{phone_number}", contact_phone)
    )


# ── Public API ────────────────────────────────────────────────────────────────

def generate_personalized_message(template_body: str, contact_name: str, contact_phone: str) -> str:
    """
    Personalize an outbound message.
    With AI: rewrites to sound natural and human.
    Without AI: replaces {name} / {phone_number} placeholders.
    """
    prompt = (
        f"Rewrite this WhatsApp message to sound warm, natural, and human. "
        f"Keep it short (1-3 sentences). No hashtags, no bullet points. "
        f"Return ONLY the final message.\n\n"
        f"Original: {template_body}\n"
        f"Recipient name: {contact_name}"
    )

    if getattr(settings, "GEMINI_API_KEY", "").strip() and _gemini_allowed():
        try:
            return _call_gemini(prompt)
        except Exception as exc:
            logger.warning("Gemini personalization failed: %s", _safe_error(exc))

    if getattr(settings, "GROQ_API_KEY", "").strip():
        try:
            return _call_groq(prompt, max_tokens=200)
        except Exception as exc:
            logger.warning("Groq personalization failed: %s", _safe_error(exc))

    client = _get_openai()
    if client:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini", max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.warning("OpenAI personalization failed: %s", _safe_error(exc))

    return _plain(template_body, contact_name, contact_phone)


def generate_followup(sender_name: str, business_context: str = "") -> str:
    """
    Generate a polite follow-up for a contact who hasn't replied in 3+ hours.
    """
    context_section = f"\nYour business: {business_context}" if business_context else ""
    display = _display_name(sender_name) if sender_name else "there"
    prompt = (
        f"You are a friendly WhatsApp business assistant.{context_section}\n\n"
        f'You sent a message to "{display}" earlier and haven\'t heard back.\n\n'
        f"Write a short, polite follow-up in 1-2 sentences. "
        f"Sound natural and human. No hashtags, no bullet points. Return ONLY the reply."
    )

    if getattr(settings, "GEMINI_API_KEY", "").strip() and _gemini_allowed():
        try:
            return _call_gemini(prompt)
        except Exception as exc:
            logger.warning("Gemini follow-up failed: %s", _safe_error(exc))

    if getattr(settings, "GROQ_API_KEY", "").strip():
        try:
            return _call_groq(prompt, max_tokens=100)
        except Exception as exc:
            logger.warning("Groq follow-up failed: %s", _safe_error(exc))

    client = _get_openai()
    if client:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini", max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.warning("OpenAI follow-up failed: %s", _safe_error(exc))

    return (
        f"Hi {display}! Just checking in — did you get a chance to see my last message? "
        f"Happy to help whenever you're ready. 😊"
    )


def generate_auto_reply(sender_name: str, incoming_message: str, business_context: str = "") -> str:
    """
    Generate a context-aware, human-like reply to an incoming WhatsApp message.

    With Gemini/OpenAI: truly intelligent, uses your business_context.
    Without AI keys: keyword rules cover 90% of real conversations (free, instant).
    Pass incoming_message="" when the sidebar preview wasn't captured — a warm
    opener is generated instead of returning an empty string.
    """
    context_section = f"\nYour business: {business_context}" if business_context else ""
    display = _display_name(sender_name)

    if incoming_message.strip():
        prompt = (
            f"You are a friendly WhatsApp business assistant.{context_section}\n\n"
            f'Customer named "{sender_name}" says: "{incoming_message}"\n\n'
            f"Write a warm, helpful reply in 1-2 sentences. "
            f"Sound like a real person — natural, friendly, professional. "
            f"No hashtags, no bullet points. Return ONLY the reply."
        )
    else:
        # Message text wasn't visible in the sidebar — send a warm opener
        prompt = (
            f"You are a friendly WhatsApp business assistant.{context_section}\n\n"
            f'"{sender_name}" has just sent you a WhatsApp message.\n\n'
            f"Write a short, warm greeting that acknowledges their message and asks "
            f"how you can help. 1-2 sentences, natural and human. "
            f"No hashtags. Return ONLY the reply."
        )

    if getattr(settings, "GEMINI_API_KEY", "").strip() and _gemini_allowed():
        try:
            return _call_gemini(prompt)
        except Exception as exc:
            logger.warning("Gemini auto-reply failed: %s", _safe_error(exc))

    if getattr(settings, "GROQ_API_KEY", "").strip():
        try:
            return _call_groq(prompt, max_tokens=150)
        except Exception as exc:
            logger.warning("Groq auto-reply failed: %s", _safe_error(exc))

    client = _get_openai()
    if client:
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini", max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.warning("OpenAI auto-reply failed: %s", _safe_error(exc))

    # Free keyword fallback — must always return something so callers never see ""
    if incoming_message.strip():
        kw = _keyword_reply(incoming_message, sender_name)
        if kw:
            return kw
        return f"Hi {display}! Thanks for your message — we'll get back to you shortly! 😊"
    return f"Hi {display}! Thanks for reaching out. How can I help you today? 😊"
