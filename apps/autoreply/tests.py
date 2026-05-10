from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.autoreply.models import ConversationMessage, ConversationState, LeadProfile
from apps.autoreply.services import anti_loop
from apps.autoreply.services.conversation_manager import ConversationManager
from apps.autoreply.services.handoff import activate_handoff
from apps.autoreply.services.language_detector import detect_language
from apps.autoreply.services.incoming import process_incoming_message
from apps.autoreply.services.state_machine import transition
from apps.profiles.models import GoLoginProfile
from apps.sessions.models import Conversation, WhatsAppSession


TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "autoreply-tests",
    }
}


@override_settings(CACHES=TEST_CACHES)
class AutoreplyConversationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="owner", password="x")
        self.profile = GoLoginProfile.objects.create(
            owner=self.user,
            name="Viraliq Sales",
            gologin_profile_id="profile-1",
            business_context="Viraliq helps businesses with leads, ads, WhatsApp, and CRM automation.",
        )
        self.conversation = Conversation.objects.create(
            profile=self.profile,
            whatsapp_jid="919999999999@c.us",
            phone="919999999999",
            display_name="Test Lead",
        )
        self.session = WhatsAppSession.objects.create(
            profile=self.profile,
            status=WhatsAppSession.Status.LOGGED_IN,
        )

    def test_language_detection_supports_hinglish_hindi_and_punjabi(self):
        self.assertEqual(detect_language("Mujhe leads chahiye"), "hinglish")
        self.assertEqual(detect_language("मुझे leads चाहिए"), "hindi")
        self.assertEqual(detect_language("ਮੈਨੂੰ leads ਚਾਹੀਦੇ"), "punjabi")

    def test_state_moves_from_problem_to_current_strategy(self):
        state = ConversationState.objects.create(conversation=self.conversation)
        lead = LeadProfile.objects.create(conversation=self.conversation, main_problem="leads")

        transition(state, lead, "need_leads")

        state.refresh_from_db()
        self.assertEqual(state.stage, ConversationState.Stage.CURRENT_STRATEGY)

    def test_processing_lock_prevents_parallel_ai_work(self):
        first = anti_loop.begin_processing(self.conversation.id)
        second = anti_loop.begin_processing(self.conversation.id)

        self.assertFalse(first.should_skip)
        self.assertTrue(second.should_skip)
        self.assertEqual(second.reason, "processing_lock")

    def test_repeated_reply_is_detected(self):
        ConversationMessage.objects.create(
            conversation=self.conversation,
            sender=ConversationMessage.Sender.AI,
            text="Got it. What kind of leads are you looking for?",
        )

        self.assertTrue(
            anti_loop.is_repeated_reply(
                self.conversation,
                "Got it. What kind of leads are you looking for?",
            )
        )

    def test_handoff_pauses_ai(self):
        activate_handoff(self.conversation, reason="user_requested_human")
        state = self.conversation.ai_state

        decision = anti_loop.should_skip(self.conversation, state, "Need pricing")

        self.assertTrue(decision.should_skip)
        self.assertEqual(decision.reason, "human_active")

    def test_manager_creates_memory_state_and_lead(self):
        result = ConversationManager().handle_incoming(
            conversation=self.conversation,
            incoming_text="I need more leads from Meta ads",
            sender_name="Test Lead",
            external_message_id="msg-1",
        )

        self.assertEqual(result["status"], "reply")
        self.assertTrue(self.conversation.ai_messages.filter(sender="user").exists())
        self.assertTrue(self.conversation.ai_messages.filter(sender="ai").exists())
        self.assertEqual(self.conversation.lead_profile.main_problem, "leads")
        self.assertEqual(self.conversation.ai_state.detected_language, "english")

    def test_phone_only_sender_creates_conversation_memory(self):
        result = process_incoming_message(
            session=self.session,
            sender_name="917347678079",
            sender_phone="",
            jid="",
            preview="I need more leads",
        )

        conversation = Conversation.objects.get(
            profile=self.profile,
            whatsapp_jid="917347678079@c.us",
        )
        self.assertEqual(result, "scheduled")
        self.assertTrue(conversation.ai_messages.filter(sender="user").exists())
        self.assertTrue(conversation.ai_messages.filter(sender="ai").exists())
