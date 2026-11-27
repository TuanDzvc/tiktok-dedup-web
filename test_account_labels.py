import json
import unittest
from unittest.mock import patch

import core_dedup
import server
from starlette.requests import Request


class AccountLabelsTests(unittest.TestCase):
    def test_observed_first_warning_type_two_is_not_recommendation_ineligibility(self):
        result = core_dedup._parse_account_labels([{'content':'First warning','label_type':2}])
        self.assertEqual(result['recommend_status'],'warning')
        self.assertFalse(result['is_not_recommended'])
        self.assertEqual(result['recommend_label'],'Cảnh báo lần đầu')
        self.assertEqual(result['account_labels_version'],2)

    def test_type_two_without_explicit_recommendation_label_is_only_warning(self):
        for labels in ([{'label_type':2}], [{'label_type':2,'content':'Account warning'}],
                       [{'label_type':'2','content':'Unknown policy label'}]):
            self.assertEqual(core_dedup._parse_account_labels(labels)['recommend_status'],'warning')

    def test_explicit_negative_labels_are_detected_regardless_of_enum(self):
        for content in ('Not recommended','Ineligible for recommendation',
                        'Account not eligible for recommendation','Tài khoản không được đề xuất'):
            result = core_dedup._parse_account_labels([{'content':content,'label_type':99}])
            self.assertEqual(result['recommend_status'],'not_recommended')
            self.assertTrue(result['is_not_recommended'])

    def test_explicit_ineligibility_is_not_hidden_by_first_warning(self):
        labels = [{'content':'First warning','label_type':2}, {'content':'Not recommended','label_type':7}]
        self.assertEqual(core_dedup._parse_account_labels(labels)['recommend_status'],'not_recommended')

    def test_json_and_plain_text_labels_and_malformed_data(self):
        labels = json.dumps([{'content':'First warning','label_type':2}])
        self.assertEqual(core_dedup._parse_account_labels(labels)['recommend_status'],'warning')
        self.assertEqual(core_dedup._parse_account_labels('First warning')['recommend_status'],'warning')
        self.assertEqual(core_dedup._parse_account_labels('[malformed')['recommend_status'],'warning')
        self.assertEqual(core_dedup._parse_account_labels([])['recommend_status'],'ok')


class AccountLabelHistoryTests(unittest.TestCase):
    def history(self, row):
        stamp = int(server.time.time())
        history = {'fixture':{'entries':{},'deleted':{},'account_entries':{
            'target':{'username':'target','unique_id':'target','checked_at':stamp,'region':'US',**row}},'account_deleted':{}}}
        with patch.object(server,'_extract_vip_user',return_value={'username':'fixture'}), \
             patch.object(server,'_load_check_history_unlocked',return_value=history), \
             patch.object(server,'_save_check_history_unlocked'), \
             patch.object(server,'check_accounts_batch_tikhub') as paid:
            response = server.api_get_check_history(Request({'type':'http','headers':[]}))
        paid.assert_not_called()
        return json.loads(response.body)['account_items'][0]

    def test_legacy_red_without_original_labels_becomes_unverified_warning(self):
        item = self.history({'recommend_status':'not_recommended','is_not_recommended':True})
        self.assertEqual(item['recommend_status'],'warning')
        self.assertFalse(item['is_not_recommended'])

    def test_old_first_warning_is_reclassified_from_preserved_evidence(self):
        item = self.history({'recommend_status':'not_recommended','raw_labels':[{'content':'First warning','label_type':2}]})
        self.assertEqual(item['recommend_status'],'warning')
        self.assertEqual(item['recommend_label'],'Cảnh báo lần đầu')

    def test_new_explicit_red_is_preserved(self):
        item = self.history({'recommend_status':'not_recommended','account_labels_version':2,
                            'raw_labels':[{'content':'Not recommended','label_type':2}]})
        self.assertEqual(item['recommend_status'],'not_recommended')

    def test_raw_labels_and_classifier_version_are_kept_in_saved_history(self):
        stamp = int(server.time.time())
        history = {}
        parsed = core_dedup._parse_account_labels([{'content':'First warning','label_type':2}])
        with patch.object(server,'_load_check_history_unlocked',return_value=history), \
             patch.object(server,'_save_check_history_unlocked'):
            server._record_account_check_history_for_user('fixture',[{'ok':True,'unique_id':'target',**parsed}])
        item = history['fixture']['account_entries']['target']
        self.assertEqual(item['account_labels_version'],2)
        self.assertEqual(item['raw_labels'][0]['content'],'First warning')


if __name__ == '__main__':
    unittest.main()
