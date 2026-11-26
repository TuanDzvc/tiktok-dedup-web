"""Last confirmed API balance. Failure is unknown, never a synthetic zero."""
import hashlib
import json
import math
import threading
import time
from pathlib import Path

import requests


class ApiBalanceCache:
    def __init__(self, path, usd_rate=25500, http=requests, clock=time.time):
        self.path = Path(path)
        self.usd_rate = usd_rate
        self.http = http
        self.clock = clock
        self.lock = threading.Lock()
        self.key_id = None
        self.data = None
        self.failed_at = None

    def get(self, api_key, force=False):
        started = self.clock()
        with self.lock:
            key_id = hashlib.sha256(str(api_key).encode()).hexdigest() if api_key else None
            if self.key_id != key_id:
                self.key_id, self.data, self.failed_at = key_id, None, None
                if key_id:
                    try:
                        saved = json.loads(self.path.read_text(encoding='utf-8'))
                        if saved.get('key_id') == key_id:
                            candidate = saved['data']
                            if 0 <= self.clock() - candidate['updated_at'] < 86400:
                                if all(math.isfinite(float(candidate[k])) and float(candidate[k]) >= 0
                                       for k in ('balance_usd', 'free_credit_usd', 'total_usd', 'total_vnd')):
                                    self.data = candidate
                    except (OSError, ValueError, KeyError, TypeError):
                        pass
            now = self.clock()
            if self.data and (now - self.data['updated_at'] < 15 and (not force or self.data['updated_at'] >= started)):
                return {**self.data, 'balance_stale': False}
            if self.failed_at is not None and now - self.failed_at < 3:
                return self._unavailable('retry_pending')
            error = 'api_key_missing'
            if api_key:
                try:
                    response = self.http.get('https://api.tikhub.io/api/v1/tikhub/user/get_user_info',
                                             headers={'Authorization': f'Bearer {api_key}', 'Accept': 'application/json'}, timeout=6)
                    error = f'http_{response.status_code}'
                    if response.status_code == 200:
                        user = response.json()['user_data']
                        if user.get('balance') is None:
                            raise ValueError('missing_balance')
                        balance = float(user['balance'])
                        credit = float(user.get('free_credit') or 0)
                        if not all(math.isfinite(v) and v >= 0 for v in (balance, credit)):
                            raise ValueError('invalid_balance')
                        total = round(balance + credit, 4)
                        self.data = dict(balance_usd=round(balance, 4), free_credit_usd=round(credit, 4),
                                         total_usd=total, total_vnd=int(round(total * self.usd_rate)),
                                         est_scans_left=int(total / .001), usd_rate=self.usd_rate,
                                         updated_at=self.clock(), balance_known=True, balance_stale=False)
                        self.failed_at = None
                        try:
                            self.path.parent.mkdir(parents=True, exist_ok=True)
                            tmp = self.path.with_suffix('.tmp')
                            tmp.write_text(json.dumps({'key_id': key_id, 'data': self.data}), encoding='utf-8')
                            tmp.replace(self.path)
                        except OSError:
                            pass
                        return dict(self.data)
                except Exception:
                    error = 'balance_request_failed'
            self.failed_at = self.clock()
            return self._unavailable(error)

    def _unavailable(self, error):
        if self.data and self.clock() - self.data['updated_at'] < 86400:
            return {**self.data, 'balance_stale': True, 'balance_error': error}
        return dict(balance_usd=None, free_credit_usd=None, total_usd=None, total_vnd=None,
                    est_scans_left=None, usd_rate=self.usd_rate, balance_known=False,
                    balance_stale=True, balance_error=error)
