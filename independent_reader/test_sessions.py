"""Synthetic fixtures only: no real session material is loaded."""
import unittest

import reader
from bootstrap_android import decode_cookie
from service import username_from_input


def literal(value):
    if value is None:return b'\x70'
    value=value.encode()
    return b'\x74'+len(value).to_bytes(2,'big')+value


class SessionChecks(unittest.TestCase):
    def test_cookie_literals_and_truncation(self):
        raw=bytes.fromhex('aced0005')+b'fixture'+b''.join(literal(x) for x in
            ['sessionid','synthetic-session',None,None,'tiktokv.us'])
        self.assertEqual(decode_cookie('sessionid',raw.hex()),('synthetic-session','tiktokv.us'))
        with self.assertRaises(ValueError):decode_cookie('sessionid',raw[:-2].hex())

    def test_cookie_header_injection_is_rejected(self):
        raw=bytes.fromhex('aced0005')+b''.join(literal(x) for x in
            ['sessionid','bad\r\nheader',None,None,'tiktokv.us'])
        with self.assertRaises(ValueError):decode_cookie('sessionid',raw.hex())

    def test_domain_scope_and_lookalike_hosts(self):
        state={'cookies':[{'name':'sessionid','value':'synthetic','domain':'tiktokv.us','path':'/'}]}
        self.assertEqual(reader.scoped_state(state,'api16-normal-useast5.tiktokv.us','/aweme/')['cookie'],'sessionid=synthetic')
        for host in ['api.tiktokv.eu','eviltiktokv.us','tiktokv.us.example.com']:
            self.assertEqual(reader.scoped_state(state,host,'/aweme/')['cookie'],'')

    def test_username_link_and_invalid_origin(self):
        self.assertEqual(username_from_input('https://www.tiktok.com/@seitezhm99d?lang=en'),'seitezhm99d')
        self.assertEqual(username_from_input('@seitezhm99d'),'seitezhm99d')
        with self.assertRaises(ValueError):username_from_input('https://example.com/@seitezhm99d')


if __name__=='__main__':unittest.main()
