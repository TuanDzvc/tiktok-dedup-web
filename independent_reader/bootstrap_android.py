"""Import only IDs issued to the newly installed official TikTok on local MuMu.

No Manager dependency, no account password, no cookie/token extraction.
Requires existing authorized root ADB on the local test emulator.
"""
import argparse
import copy
import json
import subprocess
import time
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

import reader

ADB = r'E:\Program Files\Netease\MuMuPlayer\nx_main\adb.exe'
SERIAL = '127.0.0.1:16384'
PACKAGE = 'com.ss.android.ugc.trill'


def decode_cookie(name, encoded):
    """Read bounded literal fields from TTNet's Java-serialized HttpCookie.

    No Java objects are instantiated. Reject unrecognized shapes.
    """
    raw=bytes.fromhex(encoded)
    if not raw.startswith(bytes.fromhex('aced0005')):
        raise ValueError('Unsupported cookie encoding')
    token=name.encode('ascii')
    needle=b'\x74'+len(token).to_bytes(2,'big')+token
    pos=raw.index(needle)+len(needle)

    def literal():
        nonlocal pos
        if pos>=len(raw):raise ValueError('Truncated cookie')
        tag=raw[pos];pos+=1
        if tag==0x70:return None
        if tag!=0x74 or pos+2>len(raw):raise ValueError('Unsupported cookie field')
        size=int.from_bytes(raw[pos:pos+2],'big');pos+=2
        if pos+size>len(raw):raise ValueError('Truncated string')
        value=raw[pos:pos+size].decode('utf-8');pos+=size
        return value

    value=literal()
    literal()  # HttpCookie.comment
    literal()  # HttpCookie.commentURL
    domain=literal()
    if not value or not domain or any(c in value for c in '\r\n;'):
        raise ValueError('Invalid cookie fields')
    return value,domain.lstrip('.').lower()


def own_cookie_jar():
    text=shell('cat',f'/data/user/0/{PACKAGE}/shared_prefs/ttnetCookieStore.xml')
    allowed={'sessionid','sessionid_ss','sid_tt','sid_guard','uid_tt','uid_tt_ss','odin_tt',
        'store-idc','store-country-code','store-country-code-src','tt-target-idc','reg-store-region',
        'passport_csrf_token','passport_csrf_token_default','msToken','tt_session_tlb_tag',
        'cmpl_token','store-country-sign','d_ticket'}
    cookies=[]
    for node in ET.fromstring(text):
        origin,sep,name=node.get('name','').rpartition('|')
        if not sep or name not in allowed:continue
        try:
            value,domain=decode_cookie(name,node.text or '')
        except (ValueError,UnicodeError):continue
        if domain!=urlsplit(origin).hostname:continue
        if domain not in {'tiktokv.us','tiktokv.eu','tiktokv.com'}:continue
        cookies.append({'name':name,'value':value,'domain':domain,'path':urlsplit(origin).path or '/'})
    if not any(c['name']=='sessionid' for c in cookies):
        raise RuntimeError('No valid signed-in session cookie found in official app')
    return cookies


def shell(*args):
    result = subprocess.run([ADB,'-s',SERIAL,'shell',*args],capture_output=True,
        text=True,encoding='utf-8',timeout=20)
    if result.returncode:
        raise RuntimeError('Android metadata read failed')
    return result.stdout.strip()


def build_state(profile='official47',include_session=True,existing=None):
    """Read a candidate only. Caller validates it before replacing the live state."""
    try:ident=shell('id')
    except RuntimeError:
        # Reconnect when MuMu has been closed/reopened; never start it or sign in for the user.
        subprocess.run([ADB,'connect',SERIAL],capture_output=True,timeout=6)
        ident=shell('id')
    if 'uid=0(' not in ident:
        # MuMu already has root_permission=true; do not change emulator settings.
        subprocess.run([ADB,'-s',SERIAL,'root'],capture_output=True,timeout=10)
        subprocess.run([ADB,'-s',SERIAL,'wait-for-device'],capture_output=True,timeout=10)
    text=shell('cat',f'/data/user/0/{PACKAGE}/shared_prefs/applog_stats.xml')
    values={n.get('name'):n.text if n.tag=='string' else n.get('value') for n in ET.fromstring(text)}
    did=str(values.get('device_id','0')); iid=str(values.get('install_id','0'))
    if not all(value.isdigit() and int(value)>0 for value in [did,iid]):
        raise RuntimeError('Official app has not obtained valid device/install IDs')
    state=copy.deepcopy(existing) if existing else {'device':reader.DeviceProfile(device_id='0').to_dict()}
    state.pop('cookies',None)
    state.update(device_id=did,iid=iid,cookie='',client_profile=profile,
        provenance={
            'source':'fresh official TikTok install on local MuMu; no Manager state',
            'package':PACKAGE,'app_version_code':values.get('version_code'),
            'imported_fields':['device_id','install_id'],
            'cookie_imported':False,'x_tt_token_imported':False,
            'openudid_source':'locally generated experiment value'})
    state['device'].update(device_id=did,
        device_type=shell('getprop','ro.product.model'),
        os_version=shell('getprop','ro.build.version.release'))
    state['client_overrides']={'brand':shell('getprop','ro.product.brand'),
        'os':shell('getprop','ro.build.version.release'),'os_api':shell('getprop','ro.build.version.sdk')}
    cdid_repo=f'/data/user/0/{PACKAGE}/files/keva/repo/com.ss.android.deviceregister.utils.Cdid/com.ss.android.deviceregister.utils.Cdid'
    candidates=set()
    for ext in ('.blk','.chk','.lxi','.mmb'):
        proc=subprocess.run([ADB,'-s',SERIAL,'exec-out','cat',cdid_repo+ext],capture_output=True,timeout=15)
        if proc.returncode==0:
            candidates.update(x.decode() for x in re.findall(rb'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}',proc.stdout))
    if len(candidates)==1:
        state['device']['cdid']=next(iter(candidates))
        state['provenance']['cdid_source']='official app Cdid repository'
    else:
        state['provenance']['cdid_source']='local experiment value; exact app Cdid unavailable'
    state.setdefault('created_at',int(time.time()))
    if include_session:
        state['cookies']=own_cookie_jar()
        state['provenance']['cookie_imported']=True
        state['provenance']['cookie_source']='official TikTok session user signed into on local MuMu'
        state['provenance']['imported_fields'].append('scoped cookie jar')
        countries=[c['value'] for c in state['cookies'] if c['name']=='store-country-code']
        if countries and re.fullmatch('[A-Za-z]{2}',countries[0]):
            state['store_region']=countries[0].lower()
    return state


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--profile',choices=list(reader.CLIENTS),default='official47')
    parser.add_argument('--include-session',action='store_true',help='Import local official-app session; never print it')
    args=parser.parse_args()
    reader.STATE=reader.ROOT/'.private'/f'android-official-{args.profile}.json'
    existing=json.loads(reader.STATE.read_text('utf-8')) if reader.STATE.exists() else None
    state=build_state(args.profile,args.include_session,existing)
    reader.save_state(state)
    print(json.dumps({'state_saved':True,'state_file':str(reader.STATE.relative_to(reader.ROOT)),
        'official_registered_ids':True,'cookies_imported':args.include_session,'tokens_imported':False,
        'cookie_count':len(state.get('cookies',[])),
        'manager_dependency':False}))


if __name__=='__main__':
    main()
