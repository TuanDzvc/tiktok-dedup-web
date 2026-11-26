"""Reader service using only our Python signer and the user's own TikTok state."""
import hashlib
import json
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit

import reader

STATE_FILE=reader.ROOT/'.private/android-official-official47.json'
PUBLIC_CACHE=reader.ROOT/'.private/public-profiles.json'
LOCK=threading.RLock()


def username_from_input(value):
    value=str(value).strip()
    if value.startswith(('https://','http://')):
        url=urlsplit(value)
        if url.hostname not in {'tiktok.com','www.tiktok.com','m.tiktok.com'} or not url.path.startswith('/@'):
            raise ValueError('Dán username hoặc link kênh dạng tiktok.com/@username.')
        value=url.path.split('/')[1][1:]
    value=value.lstrip('@')
    if not re.fullmatch(r'[A-Za-z0-9._]{1,64}',value):
        raise ValueError('Username không hợp lệ.')
    return value


class ProfileLookupFailure(ValueError):
    def __init__(self, message, code='profile_unavailable'):
        super().__init__(message)
        self.code = code if code == 'profile_not_found' else 'profile_unavailable'


def resolve(value):
    username=username_from_input(value)
    cache=json.loads(PUBLIC_CACHE.read_text('utf-8')) if PUBLIC_CACHE.exists() else {}
    entry=cache.get(username.lower(),{})
    if entry.get('cachedAt',0)>time.time()-600 and entry.get('profile') and 'followers' in entry['profile']:
        return entry['profile']
    proc=subprocess.run(['node',str(reader.ROOT/'resolve_public.cjs'),username],
        cwd=reader.ROOT,capture_output=True,text=True,encoding='utf-8',timeout=55)
    try: result=json.loads(proc.stdout)
    except ValueError:raise ValueError('Bộ đọc hồ sơ chưa chạy được; kiểm tra Chrome và Node.js.') from None
    if proc.returncode or not result.get('secUid'):
        raise ProfileLookupFailure(result.get('error','Không tìm được kênh.'), result.get('code'))
    cache[username.lower()]={'cachedAt':time.time(),'profile':result}
    PUBLIC_CACHE.write_text(json.dumps(cache,ensure_ascii=False,indent=2),'utf-8')
    return result


class ReadFailure(ValueError):
    def __init__(self,message,code='reader_unavailable'):
        super().__init__(message);self.code=code


def fetch_raw(value,count=18,cursor=0,state_override=None):
    # Serialize use of the isolated browser and local cache.
    with LOCK:
        profile=resolve(value)
        state=state_override if state_override is not None else json.loads(STATE_FILE.read_text('utf-8'))
        cookies=state.get('cookies',[])
        domains={c['domain'] for c in cookies if c['name']=='sessionid'}
        if 'tiktokv.us' in domains:host='api16-normal-useast5.tiktokv.us'
        elif 'tiktokv.eu' in domains:host='api16-core-no1a.tiktokv.eu'
        elif 'tiktokv.com' in domains:host='api16-normal-c-alisg.tiktokv.com'
        else:raise ReadFailure('Chưa có phiên đọc phù hợp. Nạp lại phiên TikTok chính thức trên MuMu.','session_missing')
        count=max(1,min(int(count),30));cursor=max(0,int(cursor))
        response=reader.mobile(state,host,'/aweme/v1/aweme/post/',{
            'sec_user_id':profile['secUid'],'count':count,'max_cursor':cursor,'source':0,
            'sort_type':0,'user_avatar_shrink':'144_144','video_cover_shrink':'372_495','sov_client_enable':1})
        try:data=response.json()
        except ValueError:raise ReadFailure(f'TikTok không trả dữ liệu (HTTP {response.status_code}). Chưa thể kết luận trùng.') from None
        if response.status_code!=200 or data.get('status_code')!=0 or not isinstance(data.get('aweme_list'),list):
            raise ReadFailure(f"TikTok từ chối lượt đọc (HTTP {response.status_code}, mã {data.get('status_code')}).")
        if any(not isinstance(p,dict) or p.get('group_id') is None for p in data['aweme_list']):
            raise ReadFailure('Dữ liệu thiếu group_id; chưa thể kết luận trùng.','incomplete_data')
        if not data['aweme_list'] and not cursor and profile.get('videoCount',0):
            raise ReadFailure('TikTok không trả video công khai; có thể kênh riêng tư hoặc lượt đọc bị giới hạn.','target_unavailable')
        return {'profile':profile,'data':data}


def load(value,count=18,cursor=0):
        raw=fetch_raw(value,count,cursor)
        profile=raw['profile'];data=raw['data'];posts=data['aweme_list'];rows=[]
        for post in posts:
            aweme=str(post.get('aweme_id',''));group=post.get('group_id')
            status=post.get('status') or {}
            reasons=[label for key,label in [('is_prohibited','Cấm đăng'),('in_reviewing','Đang xét duyệt'),('self_see','Chỉ mình tôi')]
                if status.get(key)]
            if post.get('content_model',{}).get('standard_biz',{}).get('penalty_context',{}).get('display_penalty_type'):reasons.append('Án phạt')
            if post.get('is_nff_or_nr') is True:reasons.append('Không lên FYP')
            rows.append({'awemeId':aweme,'groupId':str(group) if group is not None else None,
                'isDup':bool(group) and str(group)!=aweme if group is not None else None,
                'desc':post.get('desc',''),'createdAt':post.get('create_time',0),
                'views':(post.get('statistics') or {}).get('play_count',0),'restriction':', '.join(reasons),
                'url':'https://www.tiktok.com/@'+profile['username']+'/video/'+aweme})
        return {'success':True,'profile':profile,'list':rows,'count':len(rows),
            'duplicates':sum(row['isDup'] is True for row in rows),
            'unknown':sum(row['isDup'] is None for row in rows),
            'restricted':sum(bool(row['restriction']) for row in rows),
            'hasMore':bool(data.get('has_more')),'maxCursor':str(data.get('max_cursor',0)),
            'postIdsHash':hashlib.sha256(','.join(row['awemeId'] for row in rows).encode()).hexdigest(),
            'signer':'Python source','session':'User official TikTok on MuMu','managerUsed':False}
