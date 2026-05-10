import hashlib
import re

from apps.autoreply.models import ConversationState
from apps.autoreply.services.prompt_registry import VIRALIQ_LEAD_QUALIFIER_PROMPT
from apps.autoreply.services.state_machine import next_question_key


QUESTION_TEXT = {
    "main_problem": {
        "english": "Got it. What main problem are you trying to solve right now?",
        "hinglish": "Samajh gaya. Abhi main problem kya solve karni hai?",
        "hindi": "Samajh gaya. Abhi aapki main problem kya hai?",
        "punjabi": "Samajh gaya. Tusi kehri main problem solve karna chaunde ho?",
    },
    "current_marketing_method": {
        "english": "Understood. How are you getting leads right now?",
        "hinglish": "Understood. Abhi leads kaise aa rahi hain?",
        "hindi": "Samajh gaya. Abhi leads kaise aa rahi hain?",
        "punjabi": "Theek aa. Hun leads kithon aa rahi ne?",
    },
    "business_type": {
        "english": "Makes sense. What kind of business are you running?",
        "hinglish": "Makes sense. Aap kaunsa business run kar rahe ho?",
        "hindi": "Theek hai. Aap kis type ka business chalate hain?",
        "punjabi": "Makes sense. Tuhada business kis type da hai?",
    },
    "company_name": {
        "english": "Nice. What is your company name?",
        "hinglish": "Nice. Company ka naam kya hai?",
        "hindi": "Achha. Aapki company ka naam kya hai?",
        "punjabi": "Vadia. Company da naam ki hai?",
    },
    "contact_name": {
        "english": "Thanks. Who should our team ask for?",
        "hinglish": "Thanks. Team kis naam se connect kare?",
        "hindi": "Dhanyavaad. Team kis naam se baat kare?",
        "punjabi": "Thanks. Team kis naam naal connect kare?",
    },
    "service_match": {
        "english": "This sounds like a fit for Viraliq. Are you more focused on leads or conversions?",
        "hinglish": "Ye Viraliq ke liye good fit lag raha hai. Focus leads pe hai ya conversions pe?",
        "hindi": "Ye Viraliq ke liye sahi fit lag raha hai. Focus leads par hai ya conversions par?",
        "punjabi": "Eh Viraliq layi good fit lagda. Focus leads te hai ya conversions te?",
    },
    "book_call": {
        "english": "Looks like a good fit. Want to book a quick call?",
        "hinglish": "Good fit lag raha hai. Quick call book karni hai?",
        "hindi": "Ye achha fit lag raha hai. Kya quick call book karein?",
        "punjabi": "Good fit lagda. Quick call book kariye?",
    },
}


def prompt_hash() -> str:
    return hashlib.sha256(VIRALIQ_LEAD_QUALIFIER_PROMPT.encode("utf-8")).hexdigest()


def generate_reply(user_text: str, memory, state, lead, intent: str, language: str, rag_context: str = "") -> str:
    key = next_question_key(state, lead)
    if key == state.last_question_key:
        key = _fallback_question_key(key, lead)

    reply = _question_for(key, language)
    if intent == "pricing":
        reply = _localized(language, "Pricing depends on your goal and volume. What business are you running?")
        key = "business_type"
    elif intent == "objection":
        reply = _localized(language, "No problem. Are you just exploring or actively looking right now?")
        key = "objection_status"
    elif state.stage == ConversationState.Stage.CTA:
        reply = _question_for("book_call", language)
        key = "book_call"

    reply = validate_reply(reply)
    state.last_question_key = key
    state.save(update_fields=["last_question_key", "updated_at"])
    return reply


def generate_safe_fallback_reply(user_text: str, language: str = "english") -> str:
    lower = (user_text or "").lower()
    if "what are you talking" in lower or "change context" in lower:
        return _localized_context_reset(language)
    if "ok" == lower.strip():
        return _question_for("business_type", language)
    return _question_for("main_problem", language)


def validate_reply(reply: str) -> str:
    reply = re.sub(r"\s+", " ", (reply or "").strip())
    words = reply.split()
    if len(words) > 42:
        reply = " ".join(words[:40]).rstrip(" ,") + "?"
    if reply.count("?") > 1:
        first, *_ = reply.split("?")
        reply = first.strip() + "?"
    return reply


def _question_for(key: str, language: str) -> str:
    options = QUESTION_TEXT.get(key) or QUESTION_TEXT["main_problem"]
    return options.get(language) or options.get("english")


def _fallback_question_key(current_key: str, lead) -> str:
    for key in ("business_type", "company_name", "contact_name", "current_marketing_method", "book_call"):
        if key != current_key and (key == "book_call" or not getattr(lead, key, "")):
            return key
    return "book_call"


def _localized(language: str, english: str) -> str:
    if language == "hinglish":
        return "Pricing goal aur volume par depend karti hai. Aap kaunsa business run kar rahe ho?"
    if language == "hindi":
        return "Pricing goal aur volume par depend karti hai. Aap kis type ka business chalate hain?"
    if language == "punjabi":
        return "Pricing goal te volume te depend kardi aa. Tuhada business kis type da hai?"
    return english


def _localized_context_reset(language: str) -> str:
    if language == "hinglish":
        return "You're right, let me reset. Viraliq helps with leads, ads, and automation. What are you looking for?"
    if language == "hindi":
        return "Aap sahi keh rahe hain, main reset karta hoon. Aapko leads, ads ya automation mein help chahiye?"
    if language == "punjabi":
        return "Tusi sahi keh rahe ho, main reset karda haan. Leads, ads ya automation ch help chahidi?"
    return "You're right, let me reset. Are you looking for help with leads, ads, or automation?"
