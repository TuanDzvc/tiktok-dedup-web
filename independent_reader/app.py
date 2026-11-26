"""Local test UI, separate from production web. No TikTok Manager imports."""
import argparse
import json
import hmac
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import service
from session_manager import MANAGER,bridge_key


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def respond(self,status,body,content_type='application/json; charset=utf-8'):
        if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.end_headers();self.wfile.write(body)

    def do_GET(self):
        if self.path=='/':
            return self.respond(200,(service.reader.ROOT/'ui.html').read_bytes(),'text/html; charset=utf-8')
        if self.path=='/health':
            return self.respond(200,{'appId':'independent-tiktok-reader','ready':service.STATE_FILE.exists(),'managerUsed':False})
        if self.path=='/internal/status':
            if not self.authorized():return self.respond(403,{'error':'Forbidden'})
            return self.respond(200,MANAGER.view())
        self.respond(404,{'error':'Không có đường dẫn này.'})

    def do_POST(self):
        if self.path.startswith('/internal/'):
            if not self.authorized():return self.respond(403,{'error':'Forbidden'})
            if self.path=='/internal/refresh':
                threading.Thread(target=MANAGER.refresh,kwargs={'force':True},daemon=True).start()
                return self.respond(202,{'success':True,'message':'Đã bắt đầu đọc và kiểm tra phiên từ MuMu.'})
            if self.path=='/internal/check-session':
                threading.Thread(target=MANAGER.check_current,daemon=True).start()
                return self.respond(202,{'success':True,'message':'Đang kiểm tra phiên hiện tại.'})
            if self.path=='/internal/channel':
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=4096:raise ValueError('Invalid input')
                    data=json.loads(self.rfile.read(length))
                    service.username_from_input(data.get('query',''))
                    result=service.fetch_raw(data['query'],data.get('count',18),data.get('cursor',0))
                    MANAGER.read_success();return self.respond(200,{'success':True,**result})
                except service.ReadFailure as e:
                    MANAGER.read_failure(e.code)
                    return self.respond(503,{'success':False,'code':e.code,'error':str(e)})
                except ValueError as e:
                    msg=str(e).strip()[:200] or 'Không đọc được hồ sơ hoặc đầu vào không hợp lệ.'
                    return self.respond(400,{'success':False,'code':'profile_unavailable','error':msg})
                except Exception:
                    MANAGER.read_failure()
                    return self.respond(503,{'success':False,'code':'reader_unavailable','error':'Reader tạm không dùng được.'})
            return self.respond(404,{'error':'Unknown operation'})
        if self.path!='/api/check':return self.respond(404,{'error':'Không có đường dẫn này.'})
        origin=self.headers.get('Origin')
        if origin and (urlsplit(origin).hostname not in {'localhost','127.0.0.1'} or urlsplit(origin).port!=self.server.server_port):
            return self.respond(403,{'error':'Chỉ dùng giao diện tại máy này.'})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=4096:raise ValueError('Yêu cầu không hợp lệ.')
            if not self.headers.get('Content-Type','').startswith('application/json'):
                raise ValueError('Yêu cầu phải là JSON.')
            data=json.loads(self.rfile.read(length))
            result=service.load(data.get('username',''),data.get('count',18),data.get('cursor',0))
            self.respond(200,result)
        except ValueError as e:self.respond(400,{'error':str(e)})
        except FileNotFoundError:self.respond(503,{'error':'Thiếu phiên đọc hoặc dependency. Xem README để khởi tạo.'})
        except Exception:self.respond(502,{'error':'Lượt đọc chưa hoàn tất. Thử lại hoặc kiểm tra phiên TikTok.'})

    def authorized(self):
        return hmac.compare_digest(self.headers.get('X-Reader-Key',''),bridge_key())


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8767)
    args=parser.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    bridge_key();MANAGER.start()
    print(f'Check Trung: http://127.0.0.1:{args.port}',flush=True)
    server.serve_forever()
