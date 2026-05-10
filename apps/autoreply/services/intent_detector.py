import re
from dataclasses import dataclass


@dataclass(frozen=True)
class IntentResult:
    intent: str
    confidence: float
    evidence: str = ""


PATTERNS = {
    "human_support": r"\b(human|agent|person|team|call me|talk to|representative)\b",
    "booking_interest": r"\b(book|demo|meeting|call|schedule|appointment|consultation)\b",
    "pricing": r"\b(price|pricing|cost|charges|fees|budget|kitna|rate|package)\b",
    "need_leads": r"\b(lead|leads|client|customers|sales|inquir|enquir)\b",
    "ads_issue": r"\b(meta ads|facebook ads|google ads|ads? not|campaign|cpc|roas|ad spend)\b",
    "automation_interest": r"\b(automation|crm|bot|whatsapp automation|workflow|follow.?up)\b",
    "objection": r"\b(later|not now|expensive|just checking|exploring|think|maybe)\b",
    "irrelevant_query": r"\b(job|career|homework|weather|news|code|technical setup|api)\b",
    "greeting": r"\b(hi|hello|hey|namaste|sat sri akal|ssa|hlo)\b",
}


def detect_intent(text: str) -> IntentResult:
    lower = (text or "").strip().lower()
    if not lower:
        return IntentResult("unknown", 0.0)

    for intent, pattern in PATTERNS.items():
        match = re.search(pattern, lower)
        if match:
            return IntentResult(intent, 0.86, match.group(0))

    return IntentResult("unknown", 0.35)
