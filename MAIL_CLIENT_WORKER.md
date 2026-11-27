# Mail worker trên máy khách Windows

Quản Lý Mail chỉ dành cho `tuandzvcc`. Render giữ duy nhất hàng đợi, thư mục Mail, khóa ghép nối và token trong object riêng của private Storage. PC chuyển tiếp API Mail tới Render; không chạy trình duyệt thay máy khách.

## Sử dụng

1. Mở Quản Lý Mail, bấm **Tải worker máy khách (.bat)**.
2. Chuyển file vào máy Windows/Sandbox muốn chạy rồi mở file. Không cần Python; dùng Windows PowerShell 5.1 và .NET Framework. Máy cần được phép kết nối mạng ra ngoài.
3. Chờ máy hiện Online trên web, chọn máy, nạp Mail/Proxy và chọn các dòng cần chạy. Bấm **Bắt Đầu**. Chromium mở trên máy đã chọn, dùng profile riêng cho mỗi lượt.
4. Lần đầu tự tải Fingerprint Chromium 148 và kiểm tra SHA256 trước khi giải nén. Các lần sau dùng runtime đã lưu trong LOCALAPPDATA. Sandbox bị xóa dữ liệu khi đóng thì lần sau tải lại.
5. CAPTCHA/MFA hoặc phương thức khôi phục khác có thể cần thao tác trong cửa sổ trên máy khách. Worker chỉ tự đọc hộp thư fviainboxes tương ứng; không bảo đảm mọi biến thể trang Microsoft đều tự động.
6. Token vượt kiểm tra Mail.Read mới được lưu. Sau đó Lấy Code/Xem 15 thư dùng Graph trên web, không cần worker. Token bị Microsoft thu hồi thì cần đăng nhập lại.

**Dừng** hủy batch đang chạy. **Ngắt worker** thu hồi khóa ghép nối và dừng công việc trên máy đó. Tải lại BAT để ghép nối lại. File BAT chứa khóa worker riêng, cần giữ riêng như thông tin truy cập. File cục bộ chứa cấu hình, job và kết quả chờ gửi được mã hóa bằng DPAPI theo tài khoản Windows hiện tại. Không có thao tác tự khởi động cùng Windows.

## Vận hành

- Chỉ HTTP proxy được worker hỗ trợ, gồm IP:PORT:USER:PASS. Proxy có xác thực được chuyển qua cầu nối loopback trên chính máy khách.
- Nếu Windows Application Control chặn Fingerprint Chromium, worker thử Microsoft Edge trong thư mục cài đặt hệ thống, chỉ khi chữ ký Microsoft hợp lệ. Log ghi rõ dùng Edge dự phòng; không tự đổi chính sách bảo mật của Windows. Nếu không có Edge hợp lệ, web báo rõ lỗi chặn trình duyệt.
- Worker poll mỗi 2 giây, không tự chuyển job sang máy khác khi mất kết nối. Hết 90 giây không có heartbeat, job chuyển trạng thái mất kết nối để admin kiểm tra/chạy lại.
- Kết quả chưa gửi được giữ trong outbox DPAPI và thử lại khi worker kết nối. Thu hồi/dừng job không lưu kết quả muộn.
- Render phải hoạt động để nhận lệnh. Render Free có thể khởi động chậm sau khi ngủ; không có bảo đảm 24/7.
- Khi triển khai lần đầu, endpoint admin `/api/outlook-oauth/migrate-local` chỉ nhận dữ liệu nếu cloud chưa có Mail, không ghi đè thư mục đã tồn tại. Không đưa file dữ liệu, mật khẩu, token hoặc file BAT đã ghép nối vào Git.

## Kiểm tra

`python -m unittest test_outlook_client_worker` kiểm tra quyền truy cập, worker riêng từng máy, hủy/thu hồi, khôi phục hàng đợi, lưu token, chống lưu nhầm tài khoản và che mật khẩu trong log. Smoke test BAT và C# CDP chạy với dịch vụ/HTML giả; chưa tương đương kiểm chứng đăng nhập Microsoft thật trong Sandbox.
