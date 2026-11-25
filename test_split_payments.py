"""Payment policy tests using synthetic users/orders and no external requests."""
import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from starlette.requests import Request
import server
from vip_subscription import restore_paid_memberships


CONFIG = dict(configured=True, price_vnd=50000, duration_days=30, checksum_key='synthetic')


class SplitPaymentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.users = {'fixture': dict(username='fixture', role='member', active=True,
                                      expires_at=0, account_balance_vnd=12345)}
        self.orders = {}
        for name in ('_save_vip_users_unlocked', '_save_vip_orders_unlocked'):
            self.stack.enter_context(patch.object(server, name))
        self.stack.enter_context(patch.object(server, '_get_payos_config', return_value=CONFIG))
        self.stack.enter_context(patch.object(server.time, 'time', return_value=1000))

    def pay(self, kind, code='1', policy='split_v1'):
        self.orders[code] = dict(username='fixture', order_code=int(code), status='PENDING',
                                 order_type=kind, billing_policy=policy, amount=50000,
                                 duration_days=30 if kind == 'vip' else 0)
        return server._fulfill_vip_order_unlocked(self.orders, self.users, int(code), 50000)

    def test_vip_does_not_credit_wallet_and_duplicate_callback_is_safe(self):
        self.assertTrue(self.pay('vip')['ok'])
        user = self.users['fixture']
        self.assertEqual(user['account_balance_vnd'], 12345)
        self.assertEqual(user['expires_at'], 1000 + 30 * 86400)
        before = copy.deepcopy(user)
        self.assertTrue(server._fulfill_vip_order_unlocked(self.orders, self.users, 1, 50000)['already_paid'])
        self.assertEqual(user, before)
        self.assertEqual(server._public_order_view(self.orders['1'])['checks_credit'], 0)

    def test_wallet_works_for_member_and_never_grants_vip(self):
        self.pay('account')
        user = self.users['fixture']
        self.assertEqual(user['account_balance_vnd'], 62345)
        self.assertEqual(user['role'], 'member')
        self.assertEqual(user['expires_at'], 0)
        self.assertFalse(server._has_vip_privilege(user))
        self.assertEqual(server._public_order_view(self.orders['1'])['duration_days'], 0)
        self.assertEqual(restore_paid_memberships(self.users, self.orders, 2000), [])
        server._fulfill_vip_order_unlocked(self.orders, self.users, 1, 50000)
        self.assertEqual(user['account_balance_vnd'], 62345)

    def test_wallet_preserves_active_or_expired_vip(self):
        for expiry in (500, 999999):
            self.users['fixture'].update(role='vip', expires_at=expiry)
            self.pay('account', str(expiry))
            self.assertEqual(self.users['fixture']['expires_at'], expiry)

    def test_explicit_vip_renewal_extends_existing_expiry(self):
        self.users['fixture'].update(role='vip', expires_at=2000)
        self.pay('vip')
        self.assertEqual(self.users['fixture']['expires_at'], 2000 + 30 * 86400)

    def test_issued_order_keeps_its_terms_after_plan_price_changes(self):
        with patch.object(server, '_get_payos_config', return_value={**CONFIG, 'price_vnd': 100000, 'duration_days': 60}):
            self.pay('vip')
        self.assertEqual(self.users['fixture']['expires_at'], 1000 + 30 * 86400)
        self.assertEqual(self.users['fixture']['account_balance_vnd'], 12345)

    def test_legacy_pending_order_keeps_original_combined_benefit(self):
        self.pay('vip', policy='')
        self.assertEqual(self.users['fixture']['account_balance_vnd'], 62345)
        self.assertEqual(self.users['fixture']['expires_at'], 1000 + 30 * 86400)

    def test_underpayment_changes_nothing(self):
        self.orders['1'] = dict(username='fixture', status='PENDING', amount=50000,
                                order_type='vip', billing_policy='split_v1', duration_days=30)
        before = copy.deepcopy(self.users)
        result = server._fulfill_vip_order_unlocked(self.orders, self.users, 1, 49999)
        self.assertFalse(result['ok'])
        self.assertEqual(self.users, before)
        self.assertEqual(self.orders['1']['status'], 'PENDING')


class CreateOrderTests(unittest.IsolatedAsyncioTestCase):
    async def create(self, body):
        encoded = json.dumps(body).encode()
        async def receive():
            return {'type': 'http.request', 'body': encoded}
        req = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': []}, receive)
        orders = {}
        with ExitStack() as stack:
            for name, value in [('_extract_vip_user', dict(username='fixture', role='member')),
                                ('_get_payos_config', CONFIG), ('_load_vip_orders_unlocked', orders),
                                ('_build_qr_data_uri', ''), ('_payos_sign_data', 'synthetic-signature'),
                                ('_public_base_url_from_request', 'https://fixture.invalid')]:
                stack.enter_context(patch.object(server, name, return_value=value))
            stack.enter_context(patch.object(server, '_save_vip_orders_unlocked'))
            create = stack.enter_context(patch.object(server, '_create_payos_payment_sync', return_value={'qrCode': 'synthetic-qr'}))
            result = await server.api_vip_payos_create_order(req)
            return result.status_code, json.loads(result.body), create.call_count

    async def test_each_product_has_distinct_snapshot_and_benefits(self):
        for kind in ('vip', 'account'):
            status, data, calls = await self.create(dict(order_type=kind, amount=50000, force_new=True))
            self.assertEqual(status, 200)
            order = data['order']
            self.assertEqual(order['billing_policy'], 'split_v1')
            self.assertEqual(order['order_type'], kind)
            self.assertEqual(order['duration_days'], 30 if kind == 'vip' else 0)
            self.assertEqual(order['wallet_credit_vnd'], 0 if kind == 'vip' else 50000)
            self.assertEqual(calls, 1)

    async def test_invalid_or_unselected_product_and_wrong_vip_price_are_rejected(self):
        for body in ({'amount': 50000}, {'order_type': 'combined', 'amount': 50000},
                     {'order_type': 'vip', 'amount': 100000}, {'order_type': 'account', 'amount': 1000}):
            status, _, calls = await self.create(body)
            self.assertEqual(status, 400)
            self.assertEqual(calls, 0)


if __name__ == '__main__':
    unittest.main()
