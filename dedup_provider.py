"""Prefer the local independent reader; keep the existing engine as explicit fallback.

This module never imports the reader's session, signer, native modules or Manager.
"""
import datetime
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
import core_dedup

ROOT=Path(__file__).resolve().parent
READER_DIR=Path(os.getenv('INDEPENDENT_READER_DIR',str(ROOT/'independent_reader')))


def _first_url(obj):
    if isinstance(obj,str):return obj
    if isinstance(obj,dict):
        values=obj.get('url_list') or obj.get('urlList') or []
        return str(values[0]) if values else ''
    return ''


def format_native(payload):
    """Map raw mobile response into existing web schema; performs zero HTTP calls."""
    profile=payload['profile'];data=payload['data'];posts=data['aweme_list']
    username=profile['username'];author=next((p.get('author') for p in posts if isinstance(p.get('author'),dict)),{}) or {}
    region=core_dedup.normalize_country_code(author.get('region') or author.get('account_region') or '')
    region_display=core_dedup.format_full_country_display(region) if region else '—'
    uid=str(author.get('uid') or profile.get('uid') or '---')
    created=core_dedup.decode_snowflake_timestamp(uid) or '—'
    rows=[];ids={str(p.get('aweme_id')) for p in posts}
    for post in posts:
        if post.get('group_id') is None:raise ValueError('Missing group_id')
        aid=str(post.get('aweme_id') or '');group=str(post.get('group_id') or '')
        stats=post.get('statistics') or {};video=post.get('video') or {};status=post.get('status') or {}
        penalty=((post.get('content_model') or {}).get('standard_biz') or {}).get('penalty_context') or {}
        dup=bool(group and group not in ('0','None','null') and group!=aid)
        fyp=post.get('is_nff_or_nr') is True
        reasons=[text for key,text in [('is_prohibited','Cấm đăng'),('in_reviewing','Đang xét duyệt'),('self_see','Chỉ mình tôi')]
            if status.get(key)]
        if penalty.get('display_penalty_type'):reasons.append('Án phạt')
        if fyp:reasons.append('Không lên FYP')
        shadow=bool(reasons);ct=int(post.get('create_time') or 0)
        date=datetime.datetime.fromtimestamp(ct,datetime.timezone.utc) if ct else None
        duration=max(0,int(video.get('duration') or post.get('duration') or 0)//1000)
        phone=bool(post.get('content_original_type') is not None or post.get('shoot_tab_name') or
            (post.get('creation_info') or {}).get('creation_used_functions'))
        row={'id':aid,'awemeId':aid,'aweme_id':aid,'group_id':group,'groupId':group,
            'is_dup':dup,'is_same_channel_dup':dup and group in ids,
            'origin_url':f'https://www.tiktok.com/@{username if group in ids else "tiktok"}/video/{group}' if dup else '',
            'has_origin_link':dup,'dup_reason':'Trùng group_id với video khác' if dup else '',
            'desc':post.get('desc') or '','desc_clean':core_dedup.clean_desc_text(post.get('desc') or ''),
            'create_time':ct,'date_utc':date.strftime('%d/%m/%Y %H:%M') if date else '',
            'date_local':date.astimezone(datetime.timezone(datetime.timedelta(hours=7))).strftime('%d/%m/%Y %H:%M') if date else '',
            'device':'phone' if phone else 'web','source':'Phone' if phone else 'Web (PC)',
            'source_icon':'fa-mobile-screen' if phone else 'fa-desktop',
            'quality':core_dedup._extract_aweme_quality_label(video) or 'Chuẩn',
            'region':post.get('region') or region,'region_display':core_dedup.format_full_country_display(post.get('region') or region) if post.get('region') or region else '—',
            'shadow':shadow,'shadowReason':', '.join(reasons),'has_penalty':shadow,
            'is_fyp_restricted':fyp,'is_prohibited':bool(status.get('is_prohibited')),
            'is_private':bool(status.get('is_private') or status.get('self_see')),
            'is_crp_punished':False,'is_copyright':False,'is_takedown':False,
            'is_muted':bool(post.get('is_muted')),'status_code':int(post.get('status_code') or 0),
            'penalty_details':', '.join(reasons),'duration_str':f'{duration//60}:{duration%60:02d}' if duration else '',
            'cover_url':_first_url(video.get('cover') or video.get('origin_cover') or video.get('dynamic_cover')),
            'share_url':f'https://www.tiktok.com/@{username}/video/{aid}'}
        for key in ('play','digg','comment','share','collect'):
            value=int(stats.get(key+'_count') or 0);row[key+'_count']=value
            row[key+'_count_fmt']=core_dedup.format_count_compact(value)
        rows.append(row)
    bad=sum(bool(r['is_dup'] or r['shadow']) for r in rows)
    channel={'uniqueId':username,'nickname':profile.get('nickname') or author.get('nickname') or username,
        'avatar':profile.get('avatar') or _first_url(author.get('avatar_larger') or author.get('avatar_thumb')),
        'followers':int(profile.get('followers') or author.get('follower_count') or 0),
        'videoCount':int(profile.get('videoCount') or author.get('aweme_count') or 0),
        'createDate':created,'region':region,'region_display':region_display,'location_detail':''}
    channel['followers_fmt']=core_dedup.format_count_compact(channel['followers'])
    return {'success':True,'vip_mode':True,'channel':channel,'secUid':profile['secUid'],'list':rows,
        'stats_summary':{'total_videos':len(rows),'duplicate_count':sum(r['is_dup'] for r in rows),
            'penalty_count':sum(r['shadow'] for r in rows),'copyright_count':0,'clean_count':len(rows)-bad,
            'dup_percentage':round(bad/len(rows)*100,1) if rows else 0},
        'user_info':{'screen_name':username,'username':'@'+username,'create_date':created,'uid':uid,
            'mobile':'—','country':region_display,'last_nickname_change':'—','is_self':False},
        'hasMore':bool(data.get('has_more')),'maxCursor':int(data.get('max_cursor') or 0),
        'video_notice':'' if rows else 'Chưa có video công khai trong lượt đọc này.',
        '_internal_cost':{'cost_usd':0,'api_calls':0,'cached_hits':0}}


class ReaderRouter:
    def __init__(self,settings_file=None,http=None,clock=time.time):
        self.settings_file=settings_file or ROOT/'reader_settings.json'
        self.http=http or requests;self.clock=clock;self.lock=threading.RLock()
        self.blocked_until=0;self.last_error_at=0
        self.last_source='';self.last_reason='';self.last_fallback_at=0;self.fallback_count=0;self.events=[]
        self.runtime_file=self.settings_file.with_name('reader_runtime.json')
        try:
            saved=json.loads(self.runtime_file.read_text('utf-8'))
            self.last_source=str(saved.get('last_source') or '')
            self.last_reason=str(saved.get('last_reason') or '')
            self.last_fallback_at=int(saved.get('last_fallback_at') or 0)
            self.fallback_count=int(saved.get('fallback_count') or 0)
            self.events=list(saved.get('events') or [])[-20:]
        except (OSError,ValueError,TypeError):pass

    def settings(self):
        defaults={'enabled':True,'fallback_enabled':True}
        try:
            data=json.loads(self.settings_file.read_text('utf-8'))
            return {key:bool(data.get(key,value)) for key,value in defaults.items()}
        except (OSError,ValueError):return defaults

    def save_settings(self,body):
        config=self.settings()
        for key in config:
            if key in body:
                if type(body[key]) is not bool:raise ValueError('Cấu hình phải là bật/tắt.')
                config[key]=body[key]
        with self.lock:
            tmp=self.settings_file.with_suffix('.tmp');tmp.write_text(json.dumps(config),'utf-8');tmp.replace(self.settings_file)
        return config

    def _bridge(self,path,body=None):
        cloud=os.getenv('IS_CLOUD','').lower() in ('1','true') or bool(os.getenv('RENDER'))
        if cloud:raise RuntimeError('Reader only runs on local PC')
        base=os.getenv('INDEPENDENT_READER_URL','http://127.0.0.1:8767').rstrip('/')
        keyfile=Path(os.getenv('INDEPENDENT_READER_KEY_FILE',str(READER_DIR/'.private/bridge-key.txt')))
        headers={'X-Reader-Key':keyfile.read_text('ascii').strip()}
        if body is None:return self.http.get(base+path,headers=headers,timeout=(2,4),allow_redirects=False)
        return self.http.post(base+path,json=body,headers=headers,timeout=(2,85) if path.endswith('/channel') else (2,5),allow_redirects=False)

    def engine_status(self):
        try:
            response=self._bridge('/internal/status');response.raise_for_status()
            return response.json()
        except Exception:
            return {'state':'unavailable','message':'Không kết nối được reader trên PC. API dự phòng sẽ được dùng nếu đã bật.',
                'mumu_state':'unavailable','events':[]}

    def status(self):
        config=self.settings();engine=self.engine_status()
        with self.lock:
            active='independent' if config['enabled'] and engine.get('state')=='healthy' else ('api_fallback' if config['fallback_enabled'] else 'unavailable')
            return {'success':True,'settings':config,'engine':engine,'active_provider':active,
                'last_source':self.last_source,'last_reason':self.last_reason,'last_fallback_at':self.last_fallback_at,
                'fallback_count':self.fallback_count,'events':list(self.events)}

    def refresh(self,check_only=False):
        response=self._bridge('/internal/check-session' if check_only else '/internal/refresh',{})
        response.raise_for_status();return response.json()

    def _record(self,source,reason=''):
        with self.lock:
            if self.last_source!=source or self.last_reason!=reason:
                self.events=(self.events+[{'at':int(self.clock()),'source':source,'reason':reason}])[-20:]
            self.last_source=source;self.last_reason=reason
            if source=='api_fallback':self.last_fallback_at=int(self.clock());self.fallback_count+=1
            try:
                tmp=self.runtime_file.with_suffix('.tmp')
                saved={key:getattr(self,key) for key in ('last_source','last_reason','last_fallback_at','fallback_count','events')}
                tmp.write_text(json.dumps(saved,ensure_ascii=False),'utf-8');tmp.replace(self.runtime_file)
            except OSError:pass

    def check(self,legacy,query,**kwargs):
        config=self.settings();reason='reader_disabled'
        if config['enabled']:
            reason='reader_unavailable'
            try:
                if self.blocked_until>self.clock():
                    status=self.engine_status()
                    if status.get('state')!='healthy' or status.get('last_checked',0)<self.last_error_at:
                        raise RuntimeError('Reader circuit is open')
                response=self._bridge('/internal/channel',{'query':query.lstrip('!').strip(),
                    'count':kwargs.get('max_videos',18),'cursor':kwargs.get('max_cursor',0)})
                payload=response.json()
                if response.status_code!=200 or not payload.get('success'):
                    reason=payload.get('code') or reason
                    raise RuntimeError('Native read failed')
                result=format_native(payload)
                self.blocked_until=0;self._record('independent')
                result['reader_provider']={'source':'independent','fallback':False}
                return result
            except Exception:
                if reason not in {'target_unavailable','profile_unavailable','invalid_input'}:
                    self.last_error_at=self.clock();self.blocked_until=self.clock()+30
        if not config['fallback_enabled']:
            self._record('unavailable',reason)
            return {'success':False,'error':'Reader TikTok chưa dùng được và API dự phòng đang tắt.',
                'reader_provider':{'source':'unavailable','fallback':False,'reason':reason}}
        self._record('api_fallback',reason)
        result=legacy(query,**kwargs)
        if isinstance(result,dict):result['reader_provider']={'source':'api_fallback','fallback':True,'reason':reason}
        return result


ROUTER=ReaderRouter()


def start_local_supervisor():
    if os.getenv('IS_CLOUD','').lower() in ('1','true') or os.getenv('RENDER') or not (READER_DIR/'app.py').exists():return
    if os.getenv('INDEPENDENT_READER_AUTOSTART','1')=='0':return
    def loop():
        child=None;output=None
        while True:
            try:
                response=requests.get('http://127.0.0.1:8767/health',timeout=2)
                # An HTTP listener exists; never replace/kill an unknown process.
            except requests.ConnectionError:
                if child is None or child.poll() is not None:
                    if output:output.close()
                    (READER_DIR/'output').mkdir(exist_ok=True)
                    output=(READER_DIR/'output/sidecar.log').open('a',encoding='utf-8')
                    child=subprocess.Popen([sys.executable,str(READER_DIR/'app.py')],cwd=READER_DIR,
                        stdout=output,stderr=output,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            except Exception:pass
            time.sleep(10)
    threading.Thread(target=loop,name='Independent-reader-supervisor',daemon=True).start()
