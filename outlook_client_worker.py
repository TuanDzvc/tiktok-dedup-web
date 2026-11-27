"""Bound client workers; only explicitly selected mail rows are sent to a worker.

Render is the single queue/state owner. Worker keys are stored as hashes and can
be revoked; account passwords and OAuth tokens never appear in worker logs.
"""
import copy
from mail_worker_messages import localize_mail_message
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from youtube_monitor import CloudStore, MonitorError
from outlook_web_engine import CLIENT_ID, FP_BROWSER_URL, FP_BROWSER_SHA256, FP_BROWSER_VERSION, verify_saved_refresh_token


class MailCloudStore(CloudStore):
    def __init__(self, cfg, bucket):
        super().__init__(cfg, bucket)
        self.url = self.url.rsplit('/', 1)[0] + '/outlook_client_worker_v1.json'

    def load(self):
        import requests
        try:
            response = requests.get(self.url, params={'t':time.time_ns()}, headers={**self.headers,'Cache-Control':'no-cache'}, timeout=20)
            if response.status_code in (400,404) and str(response.json().get('statusCode')) == '404':
                return None
            response.raise_for_status()
            data = response.json()
            if not isinstance(data.get('folders'),dict):
                raise ValueError()
            return data
        except (requests.RequestException, ValueError):
            raise MonitorError('Không đọc được kho Mail trên cloud; chưa cho ghi đè dữ liệu.') from None


class ClientMailWorker:
    def __init__(self, service, clock=time.time, verify_token=verify_saved_refresh_token):
        self.service = service
        self.clock = clock
        self.verify_token = verify_token
        service._client_worker = self

    @property
    def state(self):
        return self.service._state

    def public_workers(self):
        return [{'id':key,'name':row.get('name') or key[:8],
                 'online':not row.get('revoked') and self.clock()-row.get('last_seen',0)<30,
                 'revoked':bool(row.get('revoked')), 'last_seen':row.get('last_seen',0)}
                for key,row in self.state.get('client_workers',{}).items()]

    def pair(self):
        with self.service._lock:
            wid,token = secrets.token_hex(12),secrets.token_urlsafe(40)
            self.state.setdefault('client_workers',{})[wid] = {'hash':hashlib.sha256(token.encode()).hexdigest(),
                'created_at':self.clock(),'last_seen':0,'name':'Máy khách '+wid[:6],'revoked':False}
            self.service._save_unlocked()
            return wid,token

    def authenticate(self, wid, token):
        row = self.state.get('client_workers',{}).get(wid)
        if not row or row.get('revoked') or not hmac.compare_digest(row.get('hash',''),hashlib.sha256(token.encode()).hexdigest()):
            raise PermissionError('Worker không hợp lệ hoặc đã thu hồi.')
        return row

    def revoke(self, wid):
        with self.service._lock:
            row=self.state.get('client_workers',{}).get(wid)
            if not row:raise ValueError('Không tìm thấy worker.')
            row['revoked']=True
            for job in self.state.get('client_jobs',{}).values():
                if job['worker_id']==wid and job['status'] in ('queued','running'):
                    job['status']='cancelled'
            self.refresh_status()
            self.service._save_unlocked()
        return self.service.get_snapshot()

    def refresh_status(self):
        batch = self.state.get('client_batch') or {}
        jobs=[j for j in self.state.get('client_jobs',{}).values() if j.get('batch_id')==batch.get('id')]
        for job in jobs:
            if job['status']=='running' and self.clock()-job.get('last_seen',0)>90:
                job['status']='lost'
            row=self.service._find_row_unlocked(job['folder'],job['row_id'])
            if row is not None:
                row['active_running']=job['status']=='running'
                if job['status']=='lost':row['status']='Mất kết nối worker'
                elif job['status']=='cancelled':row['status']='Đã dừng'
        self.service._total_count=len(jobs)
        self.service._done_count=sum(j['status'] not in ('queued','running') for j in jobs)
        self.service._ok_count=sum(j['status']=='done' for j in jobs)
        self.service._err_count=sum(j['status'] in ('error','lost') for j in jobs)
        self.service._running=any(j['status'] in ('queued','running') for j in jobs)

    def start(self, payload):
        with self.service._lock:
            self.refresh_status()
            if self.service._running:raise ValueError('Đang có lượt chạy; dừng lượt hiện tại trước.')
            online=[w for w in self.public_workers() if w['online']]
            wid=str(payload.get('worker_id') or '')
            if not wid and len(online)==1:wid=online[0]['id']
            if not any(w['id']==wid for w in online):
                raise ValueError('Chọn worker đang online. Tải và mở .bat trên máy khách/máy ảo trước.')
            if isinstance(payload.get('settings'),dict):self.service.update_settings(payload['settings'])
            folder=self.state.get('current_folder','Default')
            rows=self.state['folders'].get(folder,[])
            requested=set(payload.get('row_ids') or [])
            targets=[r for r in rows if (r['row_id'] in requested if requested else r.get('checked'))]
            if payload.get('mode')=='retry_failed':targets=[r for r in rows if r.get('status') not in ('OK','Chờ chạy')]
            if not targets:raise ValueError('Tích chọn ít nhất một mail để chạy.')
            settings=self.state.get('settings') or {}
            proxies=self.service._parsed_proxies(settings.get('proxies_text',''))
            batch=secrets.token_hex(12)
            self.state['client_batch']={'id':batch,'created_at':self.clock()}
            jobs=self.state.setdefault('client_jobs',{})
            # Old job records carry no credentials, but bound retained history.
            jobs={k:v for k,v in jobs.items() if v.get('status') in ('queued','running') or self.clock()-v.get('created_at',0)<86400}
            self.state['client_jobs']=jobs
            for index,row in enumerate(targets):
                for previous in jobs.values():
                    if previous.get('folder')==folder and previous.get('row_id')==row['row_id'] and previous['status']=='lost':
                        previous['status']='cancelled'
                if proxies and not row.get('proxy'):row['proxy']=proxies[index%len(proxies)]
                jid=secrets.token_hex(16)
                jobs[jid]={'id':jid,'batch_id':batch,'worker_id':wid,'folder':folder,'row_id':row['row_id'],
                    'status':'queued','created_at':self.clock(),'last_seen':self.clock(),'lease':secrets.token_urlsafe(24),
                    'timeout':max(60,min(600,int(settings.get('timeout') or 240))),
                    'show_browser':True}
                row['status']='Chờ worker';row['active_running']=False
            self.service._save_unlocked()
            self.service._add_log('Đã gửi '+str(len(targets))+' mail đến worker máy khách. Server không mở trình duyệt.','summary')
        return self.service.get_snapshot()

    def stop(self):
        with self.service._lock:
            for job in self.state.get('client_jobs',{}).values():
                if job['status'] in ('queued','running'):job['status']='cancelled'
            self.refresh_status();self.service._save_unlocked()
            self.service._add_log('Đã gửi lệnh dừng tới worker máy khách.','summary')
        return self.service.get_snapshot()

    def poll(self, wid, token, body):
        with self.service._lock:
            worker=self.authenticate(wid,token)
            worker['last_seen']=self.clock()
            worker['name']=str(body.get('name') or worker['name'])[:80]
            jobs=self.state.get('client_jobs',{})
            active=set(body.get('active') or [])
            for jid in active:
                job=jobs.get(jid)
                if job and job['worker_id']==wid and job['status']=='running':job['last_seen']=self.clock()
            limit=max(1,min(12,int((self.state.get('settings') or {}).get('threads') or 3)))
            claimed=[]
            running=sum(j['status']=='running' and j['worker_id']==wid for j in jobs.values())
            for job in jobs.values():
                if len(claimed)+running>=limit:break
                if job['worker_id']!=wid or job['status']!='queued':continue
                row=self.service._find_row_unlocked(job['folder'],job['row_id'])
                if row is None:job['status']='cancelled';continue
                job['status']='running';job['last_seen']=self.clock()
                job['account_fingerprint']=hashlib.sha256((str(row.get('email'))+'\0'+str(row.get('password'))).encode()).hexdigest()
                row['status']='Đang chạy trên máy khách';row['active_running']=True
                claimed.append({**job,'account':{k:row.get(k,'') for k in ('email','password','recovery_email','proxy')},'client_id':CLIENT_ID})
            if claimed or self.clock()-worker.get('persisted_at',0)>15:
                worker['persisted_at']=self.clock();self.service._save_unlocked()
            cancelled=[jid for jid in active if jid not in jobs or jobs[jid]['status']=='cancelled']
            return {'success':True,'jobs':claimed,'cancel':cancelled,'runtime':{
                'url':FP_BROWSER_URL,'sha256':FP_BROWSER_SHA256,'version':FP_BROWSER_VERSION}}

    def event(self, wid, token, body):
        with self.service._lock:
            self.authenticate(wid,token)
            job=self.state.get('client_jobs',{}).get(str(body.get('job_id') or ''))
            if not job or job['worker_id']!=wid or not hmac.compare_digest(job['lease'],str(body.get('lease') or '')):
                raise PermissionError('Lượt chạy không thuộc worker này.')
            if job['status']=='done':return {'success':True,'cancel':False,'already_done':True}
            if job['status'] not in ('running','lost'):return {'success':True,'cancel':True}
            job['last_seen']=self.clock()
            row=self.service._find_row_unlocked(job['folder'],job['row_id'])
            if row is None:return {'success':True,'cancel':True}
            kind=body.get('kind')
            if kind=='log':
                text=localize_mail_message(str(body.get('text') or ''))[:500]
                for secret in (row.get('password'),row.get('oauth2_token')):
                    if secret:text=text.replace(str(secret),'[ẩn]')
                self.service._add_log(text,'info')
                row['status']=text[:100]
            elif kind=='result':
                fingerprint=hashlib.sha256((str(row.get('email'))+'\0'+str(row.get('password'))).encode()).hexdigest()
                if job.get('account_fingerprint')!=fingerprint:raise ValueError('Mail đã bị sửa trong lúc chạy; không lưu token nhầm tài khoản.')
                before=copy.deepcopy(self.state)
                token_value=str(body.get('refresh_token') or '')
                if body.get('ok') and 20<=len(token_value)<=16000:
                    verified,_ = self.verify_token(token_value,CLIENT_ID,row.get('proxy') or '')
                    if not verified:raise ValueError('Token chưa vượt qua kiểm tra đọc Mail.Read trên web.')
                    row.update(oauth2_token=token_value,client_id=CLIENT_ID,status='OK',checked=False,active_running=False)
                    if body.get('recovery_email'):row['recovery_email']=str(body['recovery_email'])[:254]
                    job['status']='done'
                else:
                    reasons={
                        'runtime':'Không chuẩn bị được Chromium trên máy khách',
                        'browser_start':'Windows không khởi động được Chromium',
                        'browser_policy_blocked':'Windows chặn Chromium; không có Microsoft Edge đã xác thực để dự phòng',
                        'browser_exited':'Chromium tự thoát trước khi mở cổng điều khiển',
                        'browser_start_timeout':'Chromium chưa mở cổng điều khiển sau 20 giây',
                        'cdp_connect':'Không kết nối được cổng điều khiển Chromium',
                        'oauth_setup':'Lỗi khởi tạo OAuth trước khi đăng nhập',
                        'microsoft_login':'Lỗi trong luồng đăng nhập Microsoft',
                        'verification_timeout':'Hết thời gian chờ đăng nhập/xác minh',
                        'recovery_code_rejected':'Microsoft từ chối mã xác minh; cần yêu cầu mã mới đúng phiên đăng nhập',
                    }
                    reason=reasons.get(body.get('failure_code'),'Worker lỗi; chưa nhận được chi tiết từ bản cũ')
                    details=[]
                    if str(body.get('failure_line') or '').isdigit():details.append('dòng '+str(body['failure_line']))
                    if isinstance(body.get('browser_exit'),int):details.append('exit '+str(body['browser_exit']))
                    if isinstance(body.get('native_error'),int):details.append('Windows '+str(body['native_error']))
                    if details:reason+=' ('+', '.join(details)+')'
                    row.update(status=reason,active_running=False)
                    job['status']='error'
                try:self.service._save_unlocked()
                except Exception:
                    self.service._state=before
                    raise
                self.service._add_log('Worker hoàn tất một mail: '+('OK' if job['status']=='done' else reason),'summary')
                self.refresh_status()
            return {'success':True,'cancel':False}


def launcher(origin, wid, token, root, asset_origin=None):
    """A scoped pairing key, never a web JWT or mailbox password, goes in the BAT."""
    hashes={name:hashlib.sha256((root/'static'/name).read_bytes()).hexdigest()
            for name in ('mail_worker.ps1','mail_cdp_worker.cs')}
    script = r'''$ErrorActionPreference='Stop';[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;Add-Type -AssemblyName System.Security;$r=Join-Path $env:LOCALAPPDATA ('TikFlow\MailWorker\'+$env:TF_MAIL_ID);New-Item -ItemType Directory -Force -Path $r|Out-Null;$j=@{origin=$env:TF_MAIL_ORIGIN;id=$env:TF_MAIL_ID;token=$env:TF_MAIL_TOKEN}|ConvertTo-Json -Compress;$b=[Security.Cryptography.ProtectedData]::Protect([Text.Encoding]::UTF8.GetBytes($j),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser);[IO.File]::WriteAllBytes((Join-Path $r 'config.bin'),$b);$files=@{'mail_worker.ps1'='PSHASH';'mail_cdp_worker.cs'='CSHASH'};foreach($n in $files.Keys){$p=Join-Path $r $n;Invoke-WebRequest -UseBasicParsing -Uri ($env:TF_MAIL_ORIGIN+'/static/'+$n) -OutFile ($p+'.tmp');if((Get-FileHash -LiteralPath ($p+'.tmp') -Algorithm SHA256).Hash.ToLower() -ne $files[$n]){throw 'Worker hash mismatch'};Move-Item -LiteralPath ($p+'.tmp') -Destination $p -Force};Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+(Join-Path $r 'mail_worker.ps1')+'"'),'-ConfigPath',('"'+(Join-Path $r 'config.bin')+'"'))'''
    script=script.replace('PSHASH',hashes['mail_worker.ps1']).replace('CSHASH',hashes['mail_cdp_worker.cs'])
    script=script.replace("$env:TF_MAIL_ORIGIN+'/static/'", "$env:TF_MAIL_ASSETS+'/static/'")
    # Detach setup as well as the long-running worker. A BAT can briefly flash
    # when Explorer opens cmd.exe, but it must not hold that window open.
    script="try{"+script+"}catch{$r=Join-Path $env:LOCALAPPDATA ('TikFlow\\MailWorker\\'+$env:TF_MAIL_ID);New-Item -ItemType Directory -Force -Path $r|Out-Null;[IO.File]::WriteAllText((Join-Path $r 'setup-error.log'),'Worker setup failed. Check network access and download a fresh worker from the website.');exit 1}"
    return ('@echo off\r\nsetlocal\r\nset "TF_MAIL_ORIGIN='+origin+'"\r\nset "TF_MAIL_ASSETS='+(asset_origin or origin)+'"\r\nset "TF_MAIL_ID='+wid+'"\r\nset "TF_MAIL_TOKEN='+token+'"\r\n'
            'start "" powershell.exe -WindowStyle Hidden -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "'+script.replace('"','\\"')+'"\r\n'
            'exit /b 0\r\n')
