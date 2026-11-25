"""Synthetic billing tests: no payment/network requests or real data writes."""
import unittest
from unittest.mock import patch

from vip_subscription import activate_paid_membership, restore_paid_memberships


class SubscriptionTests(unittest.TestCase):
    def test_monthly_activation_preserves_wallet(self):
        user = dict(role='member', account_balance_vnd=50000)
        self.assertTrue(activate_paid_membership(user, 1000))
        self.assertEqual(user['expires_at'], 1000 + 30 * 86400)
        self.assertEqual(user['account_balance_vnd'], 50000)

    def test_active_topup_does_not_extend_but_expired_renews(self):
        user = dict(role='member')
        activate_paid_membership(user, 1000)
        end = user['expires_at']
        self.assertFalse(activate_paid_membership(user, 2000))
        self.assertEqual(user['expires_at'], end)
        self.assertTrue(activate_paid_membership(user, end))
        self.assertEqual(user['expires_at'], end + 30 * 86400)

    def test_permanent_and_admin_are_preserved(self):
        for role in ('vip', 'admin'):
            user = dict(role=role, expires_at=0)
            self.assertFalse(activate_paid_membership(user, 1000))
            self.assertEqual(user, dict(role=role, expires_at=0))

    def test_repair_uses_paid_date_once_and_never_recreates_deleted_users(self):
        users = {'fixture': dict(role='member', account_balance_vnd=34500)}
        orders = {'1': dict(username='fixture', status='PAID', amount=50000,
                            paid_at=1000, duration_days=0),
                  '2': dict(username='deleted', status='PAID', amount=50000, paid_at=1000),
                  '3': dict(username='fixture', status='PENDING', amount=50000, paid_at=5000)}
        self.assertEqual(restore_paid_memberships(users, orders, 9999), ['fixture'])
        self.assertEqual(users['fixture']['expires_at'], 1000 + 30 * 86400)
        self.assertEqual(users['fixture']['account_balance_vnd'], 34500)
        self.assertNotIn('deleted', users)
        self.assertEqual(restore_paid_memberships(users, orders, 10000), [])

    def test_server_fulfillment_is_idempotent_and_expiry_not_wallet_controls_vip(self):
        import server
        users = {'fixture': dict(role='member', active=True, account_balance_vnd=0)}
        orders = {'1': dict(username='fixture', status='PENDING', amount=50000, duration_days=30)}
        with patch.object(server, '_save_vip_users_unlocked'), \
             patch.object(server, '_save_vip_orders_unlocked'), \
             patch.object(server, '_get_payos_config', return_value={'price_vnd': 50000, 'duration_days': 30}), \
             patch.object(server.time, 'time', return_value=1000):
            self.assertFalse(server._fulfill_vip_order_unlocked(orders, users, 1, 49999)['ok'])
            self.assertTrue(server._fulfill_vip_order_unlocked(orders, users, 1, 50000)['newly_paid'])
            self.assertTrue(server._fulfill_vip_order_unlocked(orders, users, 1, 50000)['already_paid'])
            self.assertEqual(users['fixture']['account_balance_vnd'], 50000)
            self.assertTrue(server._has_vip_privilege(users['fixture']))
        with patch.object(server.time, 'time', return_value=1000 + 30 * 86400):
            self.assertFalse(server._has_vip_privilege(users['fixture']))
        self.assertFalse(server._has_vip_privilege(dict(role='member', account_balance_vnd=50000)))


if __name__ == '__main__':
    unittest.main()
