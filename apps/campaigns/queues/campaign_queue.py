def enqueue_campaign(campaign_id: int):
    """
    Queue a campaign for asynchronous execution.

    The Celery task name remains messaging.run_campaign for compatibility, but
    callers should use this boundary instead of importing task modules directly.
    """
    from apps.campaigns.tasks import run_campaign_task

    return run_campaign_task.delay(campaign_id)


def enqueue_single_message(profile_gologin_id: str, phone_number: str, message: str, log_id: int = None):
    from apps.campaigns.tasks import send_single_message_task

    return send_single_message_task.delay(profile_gologin_id, phone_number, message, log_id=log_id)
