from dataclasses import dataclass

from django.utils import timezone

from apps.autoreply.models import ConversationState, HandoffEvent


@dataclass
class HandoffDecision:
    should_handoff: bool
    reason: str = ""


def evaluate_handoff(state, lead, intent: str, text: str) -> HandoffDecision:
    lower = (text or "").lower()
    if state.human_active:
        return HandoffDecision(True, "human_active")
    if intent == "human_support":
        return HandoffDecision(True, "user_requested_human")
    if intent == "irrelevant_query":
        return HandoffDecision(True, "out_of_scope")
    if intent == "booking_interest" and lead.qualification_score >= 45:
        return HandoffDecision(True, "booking_interest")
    if lower.count("price") + lower.count("cost") + lower.count("charges") >= 2:
        return HandoffDecision(True, "pricing_repeated")
    return HandoffDecision(False)


def activate_handoff(conversation, reason: str, requested_by: str = "ai"):
    state, _ = ConversationState.objects.get_or_create(conversation=conversation)
    state.human_active = True
    state.stage = ConversationState.Stage.HUMAN_HANDOFF
    state.save(update_fields=["human_active", "stage", "updated_at"])
    event = HandoffEvent.objects.create(
        conversation=conversation,
        reason=reason,
        requested_by=requested_by,
        active=True,
    )
    return event


def resolve_handoff(conversation):
    ConversationState.objects.filter(conversation=conversation).update(
        human_active=False,
        updated_at=timezone.now(),
    )
    HandoffEvent.objects.filter(conversation=conversation, active=True).update(
        active=False,
        resolved_at=timezone.now(),
    )
