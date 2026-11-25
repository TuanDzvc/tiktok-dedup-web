"""Monthly VIP membership is independent of the account-check wallet."""


def activate_paid_membership(user, paid_at, duration_days=30):
    """Start/renew an expired membership; active top-ups never extend it."""
    if user.get('role') == 'admin':
        return False
    expires = int(user.get('expires_at') or 0)
    if user.get('role') == 'vip' and (expires <= 0 or expires > paid_at):
        return False  # Preserve manual permanent grants and active subscriptions.
    user.update(role='vip', vip_started_at=int(paid_at),
                expires_at=int(paid_at) + max(1, int(duration_days)) * 86400,
                vip_source='payment')
    return True


def restore_paid_memberships(users, orders, now, duration_days=30, minimum_amount=50000):
    """Repair missing dates using successful orders, without crediting the wallet again."""
    changed = []
    for name, user in users.items():
        if user.get('role') in ('admin', 'vip') or int(user.get('expires_at') or 0) > 0:
            continue
        paid = sorted((o for o in orders.values() if isinstance(o, dict)
                       and str(o.get('username') or '').lower() == name
                       and str(o.get('status') or '').upper() == 'PAID'
                       and str(o.get('order_type') or 'vip').lower() == 'vip'
                       and int(o.get('amount') or 0) >= minimum_amount
                       and int(o.get('paid_at') or 0) > 0),
                      key=lambda o: int(o['paid_at']))
        for order in paid:
            activate_paid_membership(user, int(order['paid_at']),
                                     int(order.get('duration_days') or duration_days))
        if paid:
            user['updated_at'] = int(now)
            changed.append(name)
    return changed
