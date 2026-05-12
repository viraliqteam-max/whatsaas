import time
import logging

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

logger = logging.getLogger(__name__)


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
                if skip.reason in ("human_active", "ai_paused"):
                    logger.info("[HumanIntervention] conversation_id=%s reason=%s profile=%s",
                                conversation.id, skip.reason,
                                getattr(conversation.profile, "gologin_profile_id", ""))
                elif skip.reason in ("echoed_ai_reply", "ai_message_loop", "repeated_reply"):
                    logger.info("[DuplicateReplyBlocked] conversation_id=%s reason=%s",
                                conversation.id, skip.reason)
                else:
                    logger.info("[ReplyCancelled] conversation_id=%s reason=%s", conversation.id, skip.reason)
                self._log_skip(conversation, None, skip.reason, started)
                return {"status": "skipped", "reason": skip.reason}

            intent_result = detect_intent(incoming_text)
            language = detect_language(incoming_text, previous=state.detected_language)
            logger.info(
                "[Intent] profile_id=%s conversation_id=%s intent=%s confidence=%s language=%s",
                getattr(conversation.profile, "gologin_profile_id", ""),
                conversation.id,
                intent_result.intent,
                intent_result.confidence,
                language,
            )
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
            emit_dashboard_event(
                "conversation.message.received",
                {
                    "profile_id": getattr(conversation.profile, "gologin_profile_id", ""),
                    "conversation_id": conversation.id,
                    "message_id": user_message.id,
                    "jid": conversation.whatsapp_jid,
                    "text": incoming_text,
                    "intent": intent_result.intent,
                    "language": language,
                },
                conversation_id=conversation.id,
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
            state.metadata = {
                **(state.metadata or {}),
                "last_detected_intent": intent_result.intent,
                "last_intent_confidence": intent_result.confidence,
                "missing_lead_fields": memory.lead_snapshot.get("missing_fields", []),
                "last_memory_message_count": len(memory.messages),
            }
            state.save(update_fields=["metadata", "updated_at"])
            logger.info(
                "[Memory] profile_id=%s conversation_id=%s messages=%s lead_score=%s missing=%s",
                getattr(conversation.profile, "gologin_profile_id", ""),
                conversation.id,
                len(memory.messages),
                lead.qualification_score,
                memory.lead_snapshot.get("missing_fields", []),
            )
            rag_context = get_rag_context(conversation, incoming_text, lead)
            business_context = getattr(conversation.profile, "business_context", "") or ""
            reply = generate_reply(
                user_text=incoming_text,
                memory=memory,
                state=state,
                lead=lead,
                intent=intent_result.intent,
                language=language,
                rag_context=rag_context,
                business_context=business_context,
            )
            logger.info(
                "[HeuristicReply] profile_id=%s conversation_id=%s intent=%s language=%s stage=%s reply_len=%d",
                getattr(conversation.profile, "gologin_profile_id", ""),
                conversation.id,
                intent_result.intent,
                language,
                state.stage,
                len(reply),
            )
            if anti_loop.is_repeated_reply(conversation, reply):
                logger.info("[DuplicateReplyBlocked] conversation_id=%s reason=repeated_reply profile=%s jid=%s",
                            conversation.id,
                            getattr(conversation.profile, "gologin_profile_id", ""),
                            conversation.whatsapp_jid)
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
                "conversation.message.ai_generated",
                {
                    "profile_id": getattr(conversation.profile, "gologin_profile_id", ""),
                    "conversation_id": conversation.id,
                    "message_id": ai_message.id,
                    "jid": conversation.whatsapp_jid,
                    "text": reply,
                    "intent": intent_result.intent,
                    "language": language,
                    "stage": state.stage,
                },
                conversation_id=conversation.id,
            )
            emit_dashboard_event(
                "conversation.message.ai_queued",
                {
                    "profile_id": getattr(conversation.profile, "gologin_profile_id", ""),
                    "conversation_id": conversation.id,
                    "message_id": ai_message.id,
                    "jid": conversation.whatsapp_jid,
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
