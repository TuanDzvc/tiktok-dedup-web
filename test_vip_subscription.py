"""Synthetic billing tests: no payment/network requests or real data writes."""
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from starlette.requests import Request
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

    def test_admin_revoked_vip_is_not_restored_by_old_paid_orders(self):
        users = {'fixture': dict(role='member', expires_at=0, vip_revoked_at=5000,
                               vip_source='admin_revoked', account_balance_vnd=34500)}
        orders = {'1': dict(username='fixture', status='PAID', order_type='vip',
                            amount=50000, paid_at=1000, duration_days=30)}
        self.assertEqual(restore_paid_memberships(users, orders, 5100), [])
        self.assertEqual(users['fixture']['role'], 'member')
        self.assertEqual(users['fixture']['expires_at'], 0)

        orders['2'] = dict(username='fixture', status='PAID', order_type='vip',
                           amount=50000, paid_at=6000, duration_days=30)
        self.assertEqual(restore_paid_memberships(users, orders, 6100), ['fixture'])
        self.assertEqual(users['fixture']['role'], 'vip')
        self.assertEqual(users['fixture']['expires_at'], 6000 + 30 * 86400)
        self.assertNotIn('vip_revoked_at', users['fixture'])


class AdminRevokeVipTests(unittest.IsolatedAsyncioTestCase):
    async def call_admin_upsert(self, body, users, now_ts=5000):
        import server
        encoded = json.dumps(body).encode()

        async def receive():
            return {'type': 'http.request', 'body': encoded}

        req = Request({'type': 'http', 'method': 'POST', 'path': '/api/vip/admin/users', 'headers': []}, receive)
        with ExitStack() as stack:
            stack.enter_context(patch.object(server, '_extract_vip_user', return_value={'username': 'admin', 'role': 'admin'}))
            stack.enter_context(patch.object(server, '_load_vip_users_unlocked', return_value=users))
            stack.enter_context(patch.object(server, '_load_blocked_devices_unlocked', return_value={}))
            stack.enter_context(patch.object(server, '_load_sync_meta_unlocked', return_value={'deleted_users': {}}))
            stack.enter_context(patch.object(server, '_save_sync_meta_unlocked'))
            stack.enter_context(patch.object(server, '_save_vip_users_unlocked'))
            stack.enter_context(patch.object(server.time, 'time', return_value=now_ts))
            resp = await server.api_vip_admin_upsert_user(req)
            return resp.status_code, json.loads(resp.body)

    async def test_admin_demote_sets_vip_revoked_at_and_blocks_old_order_restore(self):
        users = {'fixture': dict(username='fixture', role='vip', active=True,
                                 expires_at=1000 + 30 * 86400, vip_started_at=1000,
                                 vip_source='payment', account_balance_vnd=34500)}
        orders = {'1': dict(username='fixture', status='PAID', order_type='vip',
                            amount=50000, paid_at=1000, duration_days=30)}
        status, data = await self.call_admin_upsert(
            {'username': 'fixture', 'role': 'member', 'days': 0, 'active': True}, users, now_ts=5000
        )
        self.assertEqual(status, 200)
        self.assertTrue(data['success'])
        self.assertEqual(data['user']['role'], 'member')
        self.assertFalse(data['user']['is_vip'])
        self.assertEqual(data['user']['expires_at'], 0)
        self.assertEqual(users['fixture']['role'], 'member')
        self.assertEqual(users['fixture']['expires_at'], 0)
        self.assertEqual(users['fixture']['vip_revoked_at'], 5000)
        self.assertEqual(users['fixture']['vip_source'], 'admin_revoked')
        self.assertEqual(restore_paid_memberships(users, orders, 5100), [])
        self.assertEqual(users['fixture']['role'], 'member')

        status2, data2 = await self.call_admin_upsert(
            {'username': 'fixture', 'role': 'vip', 'days': 30, 'active': True}, users, now_ts=5200
        )
        self.assertEqual(status2, 200)
        self.assertTrue(data2['success'])
        self.assertEqual(users['fixture']['role'], 'vip')
        self.assertEqual(users['fixture']['vip_source'], 'admin')
        self.assertNotIn('vip_revoked_at', users['fixture'])
        self.assertEqual(users['fixture']['expires_at'], 5200 + 30 * 86400)


if __name__ == '__main__':
    unittest.main()
