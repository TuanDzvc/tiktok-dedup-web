import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import core_dedup
from account_country_evidence import country_evidence


TEXAS = 'https://p16-common-sign.tiktokcdn-us.com/tos-useast8-p-0068-tx/sample.jpeg'


class EvidenceTests(unittest.TestCase):
    def test_author_country_has_priority_over_storage_hint(self):
        self.assertEqual(country_evidence([{'author':{'region':'VN'},'cover_url':TEXAS}]), ('VN','author_profile'))

    def test_existing_us_media_inference_is_shared(self):
        self.assertEqual(country_evidence([{'cover_url':TEXAS}]), ('US','media_storage_us'))

    def test_unknown_or_display_default_is_not_evidence(self):
        for row in ({'region':'US'}, {'cover_url':'https://evil.test/tos-useast8/x.jpg'},
                    {'cover_url':'https://p16.tiktokcdn.com/ordinary/x.jpg'}):
            self.assertEqual(country_evidence([row]), ('',''))


class SharedCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        patches = [patch.object(core_dedup,'ACCOUNT_COUNTRY_CACHE_FILE',root/'account_country_cache.json'),
                   patch.object(core_dedup,'_ACCOUNT_COUNTRY_CACHE_LOADED',False),
                   patch.object(core_dedup,'_ACCOUNT_COUNTRY_CACHE',{'by_handle':{},'by_uid':{}})]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        history = {'fixture':{'entries':{
            'netflixfixture':{'region':'US','videos':[{'cover_url':TEXAS}], 'user_info':{'uid':'7493008561440261166'}},
            'unknownfixture':{'region':'US','videos':[{'region':'US'}]}}}}
        (root/'check_history.json').write_text(json.dumps(history),encoding='utf-8')

    def test_country_restored_by_handle_and_uid_without_copying_default_us(self):
        self.assertEqual(core_dedup._get_cached_account_country('netflixfixture'),'US')
        self.assertEqual(core_dedup._get_cached_account_country('', '7493008561440261166'),'US')
        self.assertEqual(core_dedup._get_cached_account_country('unknownfixture'),'')

    def test_account_check_uses_channel_country_without_extra_paid_or_cookie_calls(self):
        response = Mock(status_code=200,content=b'json')
        response.json.return_value = {'code':200,'data':{'user':{
            'uid':'7493008561440261166','unique_id':'netflixfixture','aweme_count':5,'sec_uid':'synthetic'}}}
        with patch.object(core_dedup,'_resolve_bulk_account_handle',return_value='netflixfixture'), \
             patch.object(core_dedup.requests,'get',return_value=response) as request, \
             patch.object(core_dedup,'_fetch_account_country_free_borrowed') as borrowed:
            result = core_dedup.check_single_account_profile_tikhub('netflixfixture',api_key='synthetic')
        self.assertEqual(result['region'],'US')
        self.assertEqual(result['_api_calls'],1)
        self.assertTrue(result['_cached_country_hit'])
        request.assert_called_once()
        borrowed.assert_not_called()


class GeoFilterTests(unittest.TestCase):
    def test_empty_posts_report_specific_geo_filter(self):
        import sys
        sys.path.insert(0,str(Path(__file__).parent/'independent_reader'))
        import service
        response = Mock(status_code=200)
        response.json.return_value = {'status_code':0,'aweme_list':[],
            'filter_reasons':{'1':'copyright_geo_filter','2':'copyright_geo_filter'}}
        state = {'cookies':[{'name':'sessionid','domain':'tiktokv.us','value':'synthetic'}]}
        with patch.object(service,'resolve',return_value={'username':'fixture','secUid':'synthetic','videoCount':2}), \
             patch.object(service.reader,'mobile',return_value=response):
            with self.assertRaises(service.ReadFailure) as caught:
                service.fetch_raw('fixture',state_override=state)
        self.assertEqual(caught.exception.code,'geo_restricted')
        self.assertIn('copyright_geo_filter',str(caught.exception))


class HistoryCountryTests(unittest.TestCase):
    def test_missing_country_is_enriched_without_new_check_or_timestamp_change(self):
        import server
        from starlette.requests import Request
        checked_at = int(server.time.time())
        saved = {'username':'netflixfixture','unique_id':'netflixfixture','uid':'123',
                 'region':'','region_display':'Không rõ','checked_at':checked_at}
        history = {'fixture':{'entries':{},'deleted':{},'account_entries':{'netflixfixture':saved},'account_deleted':{}}}
        with patch.object(server,'_extract_vip_user',return_value={'username':'fixture'}), \
             patch.object(server,'_load_check_history_unlocked',return_value=history), \
             patch.object(server,'_save_check_history_unlocked') as save, \
             patch.object(core_dedup,'_get_cached_account_country',return_value='US'), \
             patch.object(server,'check_accounts_batch_tikhub') as paid:
            response = server.api_get_check_history(Request({'type':'http','headers':[]}))
        item = json.loads(response.body)['account_items'][0]
        self.assertEqual(item['region'],'US')
        self.assertEqual(item['checked_at'],checked_at)
        self.assertEqual(saved['region'],'')
        paid.assert_not_called()
        save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
