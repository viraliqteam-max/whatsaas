from dataclasses import dataclass


@dataclass
class ConversationMemory:
    messages: list
    lead_snapshot: dict


def build_memory(conversation, lead, limit: int = 12) -> ConversationMemory:
    rows = list(conversation.ai_messages.order_by("-created_at", "-id")[:limit])
    rows.reverse()
    messages = [
        {
            "role": "assistant" if row.sender == "ai" else "user",
            "content": row.text,
            "sender": row.sender,
            "intent": row.intent,
            "language": row.language,
        }
        for row in rows
        if row.sender in ("user", "ai", "human")
    ]
    lead_snapshot = {
        "company_name": lead.company_name,
        "contact_name": lead.contact_name,
        "role": lead.role,
        "business_type": lead.business_type,
        "main_problem": lead.main_problem,
        "current_marketing_method": lead.current_marketing_method,
        "website": lead.website,
        "matched_service": lead.matched_service,
        "qualification_score": lead.qualification_score,
        "is_qualified": lead.is_qualified,
        "missing_fields": [
            field for field in (
                "company_name",
                "contact_name",
                "business_type",
                "main_problem",
                "current_marketing_method",
                "matched_service",
            )
            if not getattr(lead, field)
        ],
    }
    return ConversationMemory(messages=messages, lead_snapshot=lead_snapshot)
