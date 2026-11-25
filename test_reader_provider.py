"""Synthetic provider/session transitions. Never consumes paid API or real sessions."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import dedup_provider

sys.path.insert(0,str(Path(__file__).parent/'independent_reader'))
from session_manager import SessionManager


def native_payload():
    return {'success':True,'profile':{'username':'fixture','secUid':'synthetic','videoCount':1},
        'data':{'status_code':0,'aweme_list':[{'aweme_id':'123','group_id':'456','create_time':0,
            'is_nff_or_nr':True,'status':{'review_result':{'review_status':0}},'statistics':{'play_count':5}}],'has_more':False,'max_cursor':0}}


class Response:
    def __init__(self,data,status=200):self.data=data;self.status_code=status
    def json(self):return self.data
    def raise_for_status(self):
        if self.status_code>=400:raise RuntimeError('HTTP error')


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.router=dedup_provider.ReaderRouter(self.root/'settings.json',clock=lambda:1000)
        self.legacy=Mock(return_value={'success':True,'list':[]})
    def tearDown(self):self.temp.cleanup()

    def test_native_success_never_calls_paid_fallback(self):
        self.router._bridge=Mock(return_value=Response(native_payload()))
        result=self.router.check(self.legacy,'fixture')
        self.legacy.assert_not_called()
        self.assertEqual(result['_internal_cost']['api_calls'],0)
        self.assertTrue(result['list'][0]['is_dup'])
        self.assertTrue(result['list'][0]['is_fyp_restricted'])
        self.assertEqual(result['reader_provider']['source'],'independent')

    def test_failure_falls_back_once_and_is_reported(self):
        self.router._bridge=Mock(return_value=Response({'success':False,'code':'reader_unavailable'},503))
        result=self.router.check(self.legacy,'fixture',max_cursor=99)
        self.legacy.assert_called_once_with('fixture',max_cursor=99)
        self.assertTrue(result['reader_provider']['fallback'])
        self.assertEqual(self.router.fallback_count,1)

    def test_reader_and_fallback_are_always_enabled(self):
        self.router.settings_file.write_text(json.dumps({'enabled':False,'fallback_enabled':False}))
        self.assertEqual(self.router.settings(),{'enabled':True,'fallback_enabled':True})
        self.assertEqual(self.router.save_settings({'enabled':False,'fallback_enabled':False}),{'enabled':True,'fallback_enabled':True})
        self.router._bridge=Mock(side_effect=RuntimeError('offline'))
        self.assertTrue(self.router.check(self.legacy,'fixture')['reader_provider']['fallback'])
        self.legacy.assert_called_once()

    def test_healthy_refreshed_session_closes_circuit(self):
        self.router.blocked_until=1030;self.router.last_error_at=999
        self.router._bridge=Mock(side_effect=[Response({'state':'healthy','last_checked':1000}),Response(native_payload())])
        result=self.router.check(self.legacy,'fixture')
        self.assertEqual(result['reader_provider']['source'],'independent')
        self.legacy.assert_not_called()

    def test_failed_legacy_result_still_marks_fallback(self):
        self.router._bridge=Mock(side_effect=RuntimeError('offline'))
        self.legacy.return_value={'success':False,'error':'synthetic failure'}
        self.assertTrue(self.router.check(self.legacy,'fixture')['reader_provider']['fallback'])

    def test_video_link_keeps_original_single_video_scope(self):
        self.router._bridge=Mock()
        query='https://www.tiktok.com/@fixture/video/1234567890123456789'
        result=self.router.check(self.legacy,query)
        self.router._bridge.assert_not_called()
        self.legacy.assert_called_once_with(query)
        self.assertEqual(result['reader_provider']['reason'],'video_or_short_link')

    def test_thumbnail_prefers_original_signed_jpeg_over_heic(self):
        payload=native_payload()
        heic='https://images.example/cover.heic?signature=heic-value'
        jpeg='https://images.example/cover.jpeg?signature=jpeg-value'
        payload['data']['aweme_list'][0]['video']={'cover':{'url_list':[heic,jpeg]}}
        self.assertEqual(dedup_provider.format_native(payload)['list'][0]['cover_url'],jpeg)

    def test_thumbnail_checks_other_cover_fields(self):
        payload=native_payload()
        jpeg='https://images.example/original.jpeg?signature=unchanged'
        payload['data']['aweme_list'][0]['video']={'cover':{'uri':'present-but-no-urls'},'origin_cover':{'url_list':[jpeg]}}
        self.assertEqual(dedup_provider.format_native(payload)['list'][0]['cover_url'],jpeg)


class ModerationTests(unittest.TestCase):
    def row(self, **fields):
        payload = native_payload()
        payload['data']['aweme_list'][0].update(fields)
        return dedup_provider.format_native(payload)['list'][0]

    def test_real_nff_signal_keeps_legacy_explanation_without_inventing_category(self):
        row = self.row(content_model={'standard_biz': {'penalty_context': {'ec_global_verdict': 3}}})
        self.assertTrue(row['is_fyp_restricted'])
        self.assertIn('Cần cookie chính chủ', row['penalty_details'])
        self.assertNotIn('Mã 13', row['penalty_details'])
        self.assertFalse(row['is_copyright'])

    def test_exact_reason_codes_use_existing_legacy_formatter(self):
        row = self.row(reason_codes=['121'])
        self.assertIn('Nội dung không phải nguyên tác', row['penalty_details'])
        self.assertIn('Mã 121', row['penalty_details'])

    def test_reason_title_and_failed_appeal_are_preserved(self):
        row = self.row(reason_codes=['13'], appeal_status=4,
                       penalty_reason_info_list=[{'code': '13', 'title': 'Unoriginal content'}])
        self.assertIn('Mã 13', row['penalty_details'])
        self.assertIn('Kháng xịt', row['penalty_details'])

    def test_copyright_status_is_not_replaced_by_generic_nff(self):
        row = self.row(status_code=141)
        self.assertTrue(row['is_copyright'])
        self.assertIn('Vi phạm bản quyền', row['penalty_details'])

    def test_private_and_reviewing_alone_are_not_fyp_ban(self):
        row = self.row(is_nff_or_nr=False, status={'self_see': True, 'in_reviewing': True},
                       content_model={'standard_biz': {'penalty_context': {'status': 140}}})
        self.assertFalse(row['is_fyp_restricted'])
        self.assertIn('Đang xét duyệt', row['penalty_details'])

    def test_owner_cookie_uses_native_detail_enrichment_only_when_provided(self):
        with patch.object(dedup_provider.core_dedup, 'sync_items_penalty_data_native') as enrich:
            dedup_provider.format_native(native_payload())
            enrich.assert_not_called()
            dedup_provider.format_native(native_payload(), cookie_str='synthetic-owner-cookie')
            enrich.assert_called_once()

    def test_unavailable_owner_detail_does_not_fail_a_good_read(self):
        with patch.object(dedup_provider.core_dedup, 'sync_items_penalty_data_native', side_effect=RuntimeError):
            result = dedup_provider.format_native(native_payload(), cookie_str='synthetic-owner-cookie')
        self.assertTrue(result['success'])
        self.assertIn('Cần cookie chính chủ', result['list'][0]['penalty_details'])


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.file=self.root/'state.json'
        self.old={'device_id':'111','iid':'222','cookies':[{'name':'sessionid','domain':'tiktokv.us','value':'synthetic-old'}]}
        self.new={**self.old,'cookies':[{'name':'sessionid','domain':'tiktokv.us','value':'synthetic-new'}]}
        self.file.write_text(json.dumps(self.old))
    def tearDown(self):self.temp.cleanup()
    def manager(self,probe,candidate):
        return SessionManager(self.file,probe=probe,candidate_reader=candidate,status_file=self.root/'status.json')

    def test_new_valid_session_is_automatically_swapped(self):
        probe=Mock();manager=self.manager(probe,lambda _:self.new)
        manager.refresh()
        self.assertEqual(json.loads(self.file.read_text()),self.new)
        self.assertEqual(manager.view()['state'],'healthy')
        probe.assert_called_once_with(self.new)

    def test_unusable_candidate_preserves_good_session(self):
        manager=self.manager(Mock(side_effect=ValueError('bad candidate')),lambda _:self.new)
        manager._update(state='healthy')
        manager.refresh()
        self.assertEqual(json.loads(self.file.read_text()),self.old)
        self.assertEqual(manager.view()['state'],'healthy')
        self.assertEqual(manager.view()['mumu_state'],'candidate_unusable')

    def test_mumu_offline_does_not_invalidate_saved_session(self):
        manager=self.manager(Mock(),Mock(side_effect=RuntimeError('device offline')))
        manager._update(state='healthy');manager.refresh()
        self.assertEqual(manager.view()['state'],'healthy')
        self.assertEqual(json.loads(self.file.read_text()),self.old)

    def test_unusable_current_session_is_detected(self):
        manager=self.manager(Mock(side_effect=ValueError('no data')),lambda _:self.new)
        self.assertFalse(manager.check_current())
        self.assertEqual(manager.view()['state'],'unavailable')

    def test_target_error_is_not_mislabeled_session_expired(self):
        manager=self.manager(Mock(),lambda _:self.new)
        manager._update(state='healthy');manager.read_failure('target_unavailable')
        self.assertEqual(manager.view()['state'],'healthy')


if __name__=='__main__':unittest.main()
