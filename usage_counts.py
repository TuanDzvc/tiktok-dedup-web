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


def record_account_results(user, success_count, result_count):
    ensure_usage_counts(user)
    user['account_check_count'] = max(0, int(user.get('account_check_count') or 0)) + max(0, success_count)
    # Compatibility with older peers. Never use this mixed total in the UI.
    user['scan_count'] = max(0, int(user.get('scan_count') or 0)) + max(1, result_count)
