from apps.autoreply.models import ConversationState


LEAD_FIELD_QUESTIONS = {
    "main_problem": "main_problem",
    "business_type": "business_type",
    "company_name": "company_name",
    "contact_name": "contact_name",
    "current_marketing_method": "current_marketing_method",
    "website": "website",
}


def get_or_create_state(conversation):
    state, _ = ConversationState.objects.get_or_create(conversation=conversation)
    return state


def transition(state, lead, intent: str):
    old_stage = state.stage

    if state.human_active:
        state.stage = ConversationState.Stage.HUMAN_HANDOFF
    elif intent in ("human_support", "irrelevant_query"):
        state.stage = ConversationState.Stage.HUMAN_HANDOFF
    elif intent == "booking_interest" or lead.is_qualified:
        state.stage = ConversationState.Stage.CTA
    elif not lead.main_problem:
        state.stage = ConversationState.Stage.PROBLEM_DISCOVERY
    elif not lead.current_marketing_method:
        state.stage = ConversationState.Stage.CURRENT_STRATEGY
    elif not lead.matched_service:
        state.stage = ConversationState.Stage.SERVICE_MATCHING
    elif missing_lead_field(lead):
        state.stage = ConversationState.Stage.LEAD_CAPTURE
    else:
        state.stage = ConversationState.Stage.CTA

    if state.stage != old_stage:
        state.save(update_fields=["stage", "updated_at"])
    return state


def missing_lead_field(lead):
    for field in ("business_type", "company_name", "contact_name", "current_marketing_method"):
        if not getattr(lead, field):
            return field
    return ""


def next_question_key(state, lead) -> str:
    if state.stage == ConversationState.Stage.PROBLEM_DISCOVERY:
        return "main_problem"
    if state.stage == ConversationState.Stage.CURRENT_STRATEGY:
        return "current_marketing_method"
    if state.stage == ConversationState.Stage.SERVICE_MATCHING:
        return "service_match"
    if state.stage == ConversationState.Stage.LEAD_CAPTURE:
        return missing_lead_field(lead) or "company_name"
    if state.stage == ConversationState.Stage.CTA:
        return "book_call"
    return "goal"
