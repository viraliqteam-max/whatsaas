import re


PROBLEM_KEYWORDS = {
    "leads": "leads",
    "lead": "leads",
    "conversion": "conversion",
    "ads": "ads",
    "automation": "automation",
    "crm": "automation",
}

MARKETING_KEYWORDS = {
    "meta": "Meta ads",
    "facebook": "Meta ads",
    "instagram": "Meta ads",
    "google": "Google ads",
    "referral": "Referrals",
    "seo": "SEO",
    "whatsapp": "WhatsApp",
}


def update_lead_from_message(lead, text: str, intent: str):
    lower = (text or "").lower()
    changed = []

    if intent in ("need_leads", "ads_issue", "automation_interest") and not lead.main_problem:
        lead.main_problem = {
            "need_leads": "leads",
            "ads_issue": "ads",
            "automation_interest": "automation",
        }[intent]
        changed.append("main_problem")

    if not lead.main_problem:
        for key, value in PROBLEM_KEYWORDS.items():
            if key in lower:
                lead.main_problem = value
                changed.append("main_problem")
                break

    if not lead.current_marketing_method:
        for key, value in MARKETING_KEYWORDS.items():
            if key in lower:
                lead.current_marketing_method = value
                changed.append("current_marketing_method")
                break

    website = re.search(r"https?://[^\s]+|(?:www\.)[^\s]+", text or "", flags=re.I)
    if website and not lead.website:
        lead.website = website.group(0) if website.group(0).startswith("http") else f"https://{website.group(0)}"
        changed.append("website")

    if not lead.business_type:
        business_match = re.search(r"(?:i run|we run|business is|company is|we are into)\s+(.{3,80})", lower)
        if business_match:
            lead.business_type = business_match.group(1).strip(" .")
            changed.append("business_type")

    if lead.main_problem and not lead.matched_service:
        lead.matched_service = {
            "leads": "Lead Generation",
            "ads": "Ad Creatives",
            "conversion": "Sales Conversion",
            "automation": "Automation / CRM",
        }.get(lead.main_problem, "")
        if lead.matched_service:
            changed.append("matched_service")

    score = 0
    for field in (
        lead.main_problem,
        lead.business_type,
        lead.company_name,
        lead.contact_name,
        lead.current_marketing_method,
        lead.website,
    ):
        if field:
            score += 15
    if intent == "booking_interest":
        score += 25
    lead.qualification_score = min(score, 100)
    lead.is_qualified = lead.qualification_score >= 60 or intent == "booking_interest"
    changed.extend(["qualification_score", "is_qualified"])

    if changed:
        lead.save(update_fields=sorted(set(changed + ["updated_at"])))
    return lead
