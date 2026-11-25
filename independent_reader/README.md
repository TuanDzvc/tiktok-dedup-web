# Reader TikTok riêng cho web

Source Python signer có thể đọc group_id và cờ công khai bằng phiên TikTok do chủ máy tự đăng nhập trong app chính thức MuMu. Không nạp TikTok Manager, native module hay license của Manager.

Chạy độc lập: `python app.py` tại thư mục này. Chỉ lắng nghe `127.0.0.1:8767`. `server.py` của web tự giám sát/khởi động lại process này trên PC; Render dùng API dự phòng khi không có reader PC.

Dependency PC: Python requirements của web; Node.js, Chrome; chạy `npm ci --ignore-scripts` trong thư mục reader. Source signer MIT và LICENSE nằm `vendor/tiktok-signer`, commit upstream c981a8b095cb5a7521a40b19a6f3922a8c905d59; đã sửa rotate Ladon và một số field Argus như bản thử.

## Phiên và MuMu

- State, cookie và khóa bridge chỉ ở `.private/`, không Git, không Docker, không đồng bộ peer/Supabase.
- App chính thức hiện dùng `com.ss.android.ugc.trill`, Android 15 index 0; ADB `127.0.0.1:16384`. MuMu đã bật sẵn quyền root. Không sửa APK và không tự đăng nhập/giải CAPTCHA.
- `session_manager.py` theo dõi khoảng 30 giây. Khi bạn mở MuMu và đăng nhập lại, đọc device/install IDs, Cdid và cookie mới, thử đọc dữ liệu bằng candidate rồi mới thay state bằng atomic rename.
- Candidate không dùng được không ghi đè phiên đang lưu. MuMu tắt không làm mất hiệu lực phiên đang lưu. Không kết luận tài khoản bị đình chỉ chỉ vì request rỗng.
- Trạng thái đọc được kiểm tra lại định kỳ và sau mỗi lượt đọc. Đổi nguồn/API fallback nằm ở `dedup_provider.py` của web.
- Nạp thủ công nếu cần: `python bootstrap_android.py --profile official47 --include-session`; nút Nạp phiên mới từ MuMu trên web là cách khuyến nghị vì nó kiểm tra trước khi thay phiên.

Bridge `/internal/status`, `/internal/channel`, `/internal/refresh`, `/internal/check-session` cần `X-Reader-Key` từ `.private/bridge-key.txt`. Không trả cookie/token qua bridge. Cookie được chọn theo domain/path, không tự đi theo redirect.

Hằng SDK/manifest là profile tương thích đã đọc live thành công, chưa kiểm chứng mọi endpoint/độ bền dài hạn. Bộ tra username dùng Chrome profile công khai riêng, không dùng Chrome cá nhân.
