"""Watch the user's local official TikTok app; never automate sign-in or CAPTCHA."""
import hashlib
import json
import secrets
import threading
import time

import bootstrap_android
import service

KEY_FILE=service.reader.ROOT/'.private/bridge-key.txt'
STATUS_FILE=service.reader.ROOT/'.private/engine-status.json'


def bridge_key():
    KEY_FILE.parent.mkdir(exist_ok=True)
    if not KEY_FILE.exists():
        try:
            with KEY_FILE.open('x',encoding='ascii') as file:file.write(secrets.token_urlsafe(32))
        except FileExistsError:pass
    return KEY_FILE.read_text('ascii').strip()


def fingerprint(state):
    stable={'device_id':state.get('device_id'),'iid':state.get('iid'),
        'cookies':sorted((c['domain'],c['name'],c['value']) for c in state.get('cookies',[])
            if c['name'] in {'sessionid','sessionid_ss','sid_tt','uid_tt'})}
    return hashlib.sha256(json.dumps(stable,sort_keys=True).encode()).hexdigest()


class SessionManager:
    def __init__(self,state_file=service.STATE_FILE,probe=None,candidate_reader=None,status_file=STATUS_FILE):
        self.state_file=state_file
        self.status_file=status_file
        self.probe=probe or self._probe
        self.candidate_reader=candidate_reader or (lambda existing:bootstrap_android.build_state('official47',True,existing))
        self.lock=threading.RLock();self.refresh_lock=threading.Lock()
        self.status={'state':'checking','message':'Đang kiểm tra phiên TikTok.',
            'last_checked':0,'last_good':0,'last_imported':0,'mumu_state':'not_checked',
            'generation':'','events':[],'refreshing':False}

    @staticmethod
    def _probe(state):
        result=service.fetch_raw('seitezhm99d',1,0,state_override=state)
        posts=result['data']['aweme_list']
        if not posts or any(p.get('group_id') is None for p in posts):
            raise ValueError('Probe has no usable group IDs')

    def _update(self,**fields):
        with self.lock:
            before=self.status.get('state');self.status.update(fields)
            if 'state' in fields and before!=fields['state']:
                self.status['events']=(self.status['events']+[{'at':int(time.time()),'state':fields['state'],
                    'message':self.status['message']}])[-15:]
            # Sanitized metadata only: no cookies, IDs, tokens, or exception URLs.
            try:
                tmp=self.status_file.with_suffix('.tmp')
                tmp.write_text(json.dumps(self.status,ensure_ascii=False,indent=2),'utf-8');tmp.replace(self.status_file)
            except OSError:pass

    def view(self):
        with self.lock:return json.loads(json.dumps(self.status))

    def read_success(self):
        now=int(time.time())
        self._update(state='healthy',message='Phiên TikTok đọc dữ liệu được; đang ưu tiên reader riêng.',last_checked=now,last_good=now)

    def read_failure(self,code='reader_unavailable'):
        if code in {'target_unavailable','geo_restricted','invalid_input','profile_unavailable','profile_not_found'}:return
        self._update(state='unavailable',message='Reader TikTok chưa dùng được. Chưa đủ căn cứ kết luận tài khoản bị đình chỉ.',last_checked=int(time.time()))

    def check_current(self):
        try:
            state=json.loads(self.state_file.read_text('utf-8'));self.probe(state)
            self._update(generation=fingerprint(state)[:12]);self.read_success()
            return True
        except Exception:
            self.read_failure();return False

    def refresh(self,force=False):
        if not self.refresh_lock.acquire(blocking=False):return self.view()
        self._update(refreshing=True)
        try:
            try:
                existing=json.loads(self.state_file.read_text('utf-8')) if self.state_file.exists() else None
                candidate=self.candidate_reader(existing)
            except Exception:
                self._update(mumu_state='unavailable',refresh_message='Chưa đọc được MuMu. Mở MuMu, đăng nhập TikTok rồi thử lại; phiên đang lưu vẫn được giữ.')
                return self.view()
            changed=not existing or fingerprint(candidate)!=fingerprint(existing)
            current=self.view()
            if not force and not changed and time.time()-current['last_imported']<300:
                self._update(mumu_state='connected');return self.view()
            try:self.probe(candidate)
            except Exception:
                self._update(mumu_state='candidate_unusable',refresh_message='Phiên trong MuMu chưa vượt kiểm tra đọc. Giữ nguyên phiên đang lưu; hãy hoàn tất đăng nhập/xác minh.')
                return self.view()
            # Candidate validated first. Atomic replacement preserves the old good file on failure.
            with service.LOCK:
                tmp=self.state_file.with_suffix('.tmp')
                tmp.write_text(json.dumps(candidate,ensure_ascii=False,indent=2),'utf-8');tmp.replace(self.state_file)
            self._update(last_imported=int(time.time()),generation=fingerprint(candidate)[:12],mumu_state='connected',
                refresh_message='Đã nạp và xác nhận phiên từ MuMu.' if changed else 'Đã làm mới cookie từ MuMu; đọc dữ liệu thành công.')
            self.read_success();return self.view()
        finally:
            self._update(refreshing=False);self.refresh_lock.release()

    def start(self):
        def loop():
            self.check_current()
            while True:
                try:
                    self.refresh()
                    if time.time()-self.view()['last_checked']>=60:self.check_current()
                except Exception:pass
                time.sleep(30)
        threading.Thread(target=loop,name='TikTok-session-watcher',daemon=True).start()


MANAGER=SessionManager()
