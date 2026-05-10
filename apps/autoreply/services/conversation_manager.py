import time

from django.utils import timezone

from apps.autoreply.models import AIReplyLog, ConversationMessage, ConversationState, LeadProfile
from apps.autoreply.services import anti_loop
from apps.autoreply.services.ai_generator import generate_reply, prompt_hash
from apps.autoreply.services.handoff import activate_handoff, evaluate_handoff
from apps.autoreply.services.intent_detector import detect_intent
from apps.autoreply.services.language_detector import detect_language
from apps.autoreply.services.lead_extractor import update_lead_from_message
from apps.autoreply.services.memory_builder import build_memory
from apps.autoreply.services.rag import get_rag_context
from apps.autoreply.services.state_machine import get_or_create_state, transition
from shared.services.websocket_events import emit_dashboard_event


class ConversationManager:
    def handle_incoming(
        self,
        conversation,
        incoming_text: str,
        sender_name: str = "",
        external_message_id: str = "",
    ) -> dict:
        started = time.monotonic()
        state = get_or_create_state(conversation)

        processing = anti_loop.begin_processing(conversation.id)
        if processing.should_skip:
            self._log_skip(conversation, None, processing.reason, started)
            return {"status": "skipped", "reason": processing.reason}

        try:
            skip = anti_loop.should_skip(conversation, state, incoming_text, external_message_id)
            if skip.should_skip:
                self._log_skip(conversation, None, skip.reason, started)
                return {"status": "skipped", "reason": skip.reason}

            intent_result = detect_intent(incoming_text)
            language = detect_language(incoming_text, previous=state.detected_language)
            user_message = ConversationMessage.objects.create(
                conversation=conversation,
                sender=ConversationMessage.Sender.USER,
                text=incoming_text,
                intent=intent_result.intent,
                language=language,
                external_message_id=external_message_id,
                metadata={
                    "sender_name": sender_name,
                    "intent_confidence": intent_result.confidence,
                    "intent_evidence": intent_result.evidence,
                },
            )

            lead, _ = LeadProfile.objects.get_or_create(conversation=conversation)
            lead = update_lead_from_message(lead, incoming_text, intent_result.intent)
            state.detected_language = language
            state.last_intent = intent_result.intent
            state.save(update_fields=["detected_language", "last_intent", "updated_at"])

            handoff_decision = evaluate_handoff(state, lead, intent_result.intent, incoming_text)
            if handoff_decision.should_handoff:
                event = activate_handoff(conversation, handoff_decision.reason)
                emit_dashboard_event(
                    "conversation.handoff.started",
                    {"conversation_id": conversation.id, "reason": event.reason},
                    conversation_id=conversation.id,
                )
                self._log_skip(conversation, user_message, handoff_decision.reason, started)
                return {"status": "handoff", "reason": handoff_decision.reason}

            state = transition(state, lead, intent_result.intent)
            memory = build_memory(conversation, lead)
            rag_context = get_rag_context(conversation, incoming_text, lead)
            reply = generate_reply(
                user_text=incoming_text,
                memory=memory,
                state=state,
                lead=lead,
                intent=intent_result.intent,
                language=language,
                rag_context=rag_context,
            )
            if anti_loop.is_repeated_reply(conversation, reply):
                self._log_skip(conversation, user_message, "repeated_reply", started)
                return {"status": "skipped", "reason": "repeated_reply"}

            ai_message = ConversationMessage.objects.create(
                conversation=conversation,
                sender=ConversationMessage.Sender.AI,
                text=reply,
                intent=intent_result.intent,
                language=language,
            )
            state.last_ai_reply_at = timezone.now()
            state.save(update_fields=["last_ai_reply_at", "updated_at"])
            anti_loop.mark_ai_replied(conversation.id, reply)
            AIReplyLog.objects.create(
                conversation=conversation,
                user_message=user_message,
                prompt_hash=prompt_hash(),
                reply_text=reply,
                latency_ms=self._elapsed_ms(started),
            )
            emit_dashboard_event(
                "conversation.message.ai_queued",
                {
                    "conversation_id": conversation.id,
                    "message_id": ai_message.id,
                    "stage": state.stage,
                    "lead_score": lead.qualification_score,
                },
                conversation_id=conversation.id,
            )
            return {
                "status": "reply",
                "reply": reply,
                "intent": intent_result.intent,
                "language": language,
                "stage": state.stage,
            }
        finally:
            anti_loop.end_processing(conversation.id)

    def _log_skip(self, conversation, user_message, reason: str, started: float) -> None:
        AIReplyLog.objects.create(
            conversation=conversation,
            user_message=user_message,
            prompt_hash=prompt_hash(),
            skipped_reason=reason,
            latency_ms=self._elapsed_ms(started),
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)
