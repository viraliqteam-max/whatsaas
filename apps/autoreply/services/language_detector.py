import re


def detect_language(text: str, previous: str = "english") -> str:
    text = text or ""

    if re.search(r"[\u0A00-\u0A7F]", text):
        return "punjabi"
    if re.search(r"[\u0900-\u097F]", text):
        return "hindi"

    lower = text.lower()
    punjabi_words = ("ki haal", "tusi", "mainu", "chahida", "chaida", "paaji", "veer")
    hindi_words = ("kaise", "kya", "mujhe", "chahiye", "kitna", "aap", "hai", "nahi")
    hinglish_words = ("leads chahiye", "ads nahi", "business hai", "call kar", "kya price")

    if any(word in lower for word in punjabi_words):
        return "punjabi"
    if any(word in lower for word in hinglish_words):
        return "hinglish"
    if any(word in lower for word in hindi_words):
        return "hinglish"
    return previous or "english"
