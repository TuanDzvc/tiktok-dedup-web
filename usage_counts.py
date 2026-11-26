"""Separate usage counters; preserve uncertainty in the old mixed counter."""


def ensure_usage_counts(user):
    if 'dedup_check_count' in user:
        return False
    old_total = max(0, int(user.get('scan_count') or 0))
    account = max(0, int(user.get('account_check_count') or 0))
    user['dedup_check_count'] = max(0, old_total - account)
    user['dedup_count_estimated'] = old_total > 0
    return True


def record_dedup_success(user):
    ensure_usage_counts(user)
    user['dedup_check_count'] += 1
    user['scan_count'] = max(0, int(user.get('scan_count') or 0)) + 1


def record_dedup_blocked(user):
    """A dedup attempt refused because the free daily quota was exhausted."""
    user['dedup_blocked_count'] = max(0, int(user.get('dedup_blocked_count') or 0)) + 1


DEDUP_BACKFILL_MARKER = 'dedup_backfill_v1'


def backfill_dedup_counts(users, history, quota_bucket):
    """One-time repair: never lower a counter; raise it to the best evidence available.

    Evidence = distinct channels in the user's check history and today's free quota usage.
    Returns the list of usernames whose counter was raised.
    """
    raised = []
    history = history if isinstance(history, dict) else {}
    quota_bucket = quota_bucket if isinstance(quota_bucket, dict) else {}
    for name, user in users.items():
        if not isinstance(user, dict) or user.get(DEDUP_BACKFILL_MARKER):
            continue
        ensure_usage_counts(user)
        h = history.get(name) if isinstance(history.get(name), dict) else {}
        channels = set()
        for key in ('entries', 'deleted'):
            if isinstance(h.get(key), dict):
                channels.update(h[key].keys())
        quota_used = max(0, int(quota_bucket.get(f'usr:{name}') or 0))
        evidence = max(len(channels), quota_used)
        current = max(0, int(user.get('dedup_check_count') or 0))
        if evidence > current:
            user['dedup_check_count'] = evidence
            user['dedup_count_estimated'] = True
            raised.append(name)
        user[DEDUP_BACKFILL_MARKER] = True
    return raised


def record_account_results(user, success_count, result_count):
    ensure_usage_counts(user)
    user['account_check_count'] = max(0, int(user.get('account_check_count') or 0)) + max(0, success_count)
    # Compatibility with older peers. Never use this mixed total in the UI.
    user['scan_count'] = max(0, int(user.get('scan_count') or 0)) + max(1, result_count)
