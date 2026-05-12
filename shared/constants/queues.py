CAMPAIGN_QUEUE = "campaign_queue"
INCOMING_QUEUE = "incoming_queue"
RETRY_QUEUE = "retry_queue"
DEFAULT_QUEUE = "default"

QUEUE_NAMES = (
    DEFAULT_QUEUE,
    CAMPAIGN_QUEUE,
    INCOMING_QUEUE,
    RETRY_QUEUE,
)

CAMPAIGN_TASKS = (
    "messaging.run_campaign",
    "messaging.send_single_message",
    "messaging.check_message_ack_timeout",
    "messaging.reconcile_stale_messages",
)

INCOMING_TASKS = (
    "autoreply.process_incoming_message",
    "sessions.poll_all_inboxes",
    "sessions.send_followup_messages",
)

PROFILE_TASKS = (
    "profiles.sync_gologin_profiles",
    "profiles.ensure_profile_runtime",
    "profiles.check_profile_heartbeats",
    "profiles.cleanup_stale_runtimes",
)

RETRY_TASK_PATTERNS = (
    "*.retry",
    "*.retries",
)
