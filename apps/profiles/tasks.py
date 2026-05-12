from celery import shared_task


@shared_task(bind=True, name="profiles.sync_gologin_profiles", max_retries=2)
def sync_gologin_profiles_task(self):
    from apps.profiles.services import sync_gologin_profiles

    return sync_gologin_profiles()


@shared_task(bind=True, name="profiles.ensure_profile_runtime", max_retries=2)
def ensure_profile_runtime_task(self, profile_id: str, reason: str = ""):
    from apps.profiles.services import ensure_profile_runtime

    return ensure_profile_runtime(profile_id, reason=reason)


@shared_task(bind=True, name="profiles.check_profile_heartbeats", max_retries=0)
def check_profile_heartbeats_task(self):
    from apps.profiles.services import mark_stale_profiles_unhealthy

    return {"stale": mark_stale_profiles_unhealthy()}


@shared_task(name="profiles.cleanup_stale_runtimes", max_retries=0)
def cleanup_stale_runtimes_task():
    """
    Release orphaned profile locks every 10 seconds.
    Covers: dead PIDs, launch timeouts, stale heartbeats from crashed processes.
    """
    from apps.profiles.services import cleanup_orphaned_locks

    return cleanup_orphaned_locks()
