import json
import unittest
from unittest.mock import Mock, patch

import core_dedup
from account_country_evidence import public_profile_country, honest_history_country


def html_for(user, viewer='US', biz='US-TTP'):
    scope = {'webapp.user-detail': {'statusCode': 0, 'userInfo': {'user': user}},
             'webapp.app-context': {'region': viewer},
             'webapp.biz-context': {'vregion': biz, 'vgeo': 'VGeo-US', 'domains': {'kind': 'us'}}}
    return '<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">' + json.dumps({'__DEFAULT_SCOPE__': scope}) + '</script>'


class PublicCountryTests(unittest.TestCase):
    def test_zero_video_profile_country_can_be_read_with_borrowed_cookie(self):
        response = Mock(status_code=200, text=html_for({'uniqueId': 'fixture', 'id': '123', 'region': 'VN'}))
        with patch.object(core_dedup, 'get_borrowed_cookies_pool', return_value=[('fixture-cookie', 'fixture-ua', '')]), \
             patch.object(core_dedup.requests, 'get', return_value=response) as read, \
             patch.object(core_dedup, 'fetch_external_channel_videos_creator_api') as videos:
            self.assertEqual(core_dedup._fetch_account_country_free_borrowed('fixture', 'sec', uid='123', has_videos=False), 'VN')
        self.assertEqual(read.call_count, 1)
        videos.assert_not_called()

    def test_viewer_and_business_region_never_become_account_country(self):
        self.assertEqual(public_profile_country(html_for({'uniqueId': 'fixture', 'id': '123'}), 'fixture', '123'), '')

    def test_target_country_wins_over_server_routing(self):
        self.assertEqual(public_profile_country(html_for({'uniqueId': 'fixture', 'id': '123', 'region': 'VN'}), 'fixture', '123'), 'VN')

    def test_profile_identity_is_verified(self):
        html = html_for({'uniqueId': 'someoneelse', 'id': '999', 'region': 'VN'})
        self.assertEqual(public_profile_country(html, 'fixture', '123'), '')

    def test_legacy_empty_channel_us_is_not_replayed_as_fact(self):
        item = {'username': 'fixture', 'videos': [], 'region': 'US', 'checked_at': 123,
                'channel': {'region': 'US'}, 'user_info': {'country': 'Mỹ (US)'}}
        fixed = honest_history_country(item)
        self.assertEqual(fixed['region'], '')
        self.assertEqual(fixed['channel']['region'], '')
        self.assertEqual(fixed['user_info']['country'], 'Không rõ')
        self.assertEqual(fixed['checked_at'], 123)
        self.assertEqual(item['region'], 'US')

    def test_new_profile_evidence_is_preserved(self):
        item = {'region': 'VN', 'country_source': 'profile', 'videos': []}
        self.assertEqual(honest_history_country(item), item)


if __name__ == '__main__':
    unittest.main()
