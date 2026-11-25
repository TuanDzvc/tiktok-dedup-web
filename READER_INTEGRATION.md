# Check trùng ưu tiên phiên TikTok, API dự phòng

Ngày triển khai: 25/11/2026. Luồng `/api/check` giữ nguyên xác thực, quota, phí ví khách hàng, phân quyền quét thêm, lịch sử và ảnh. Chỉ thay nguồn dữ liệu: reader riêng trước, engine API cũ làm dự phòng.

Trạng thái triển khai: PC đã cập nhật và domain trả `X-Served-By: PC-Primary`. GitHub từ chối credential đã lưu (401); người dùng chọn cập nhật PC trước, Render làm sau. Commit nằm local; Render vẫn chạy bản API cũ, chưa có bảng quản lý phiên mới khi PC tắt.

## Vận hành trên web

Hiển thị chi phí cho Admin: tab Check trùng không hiện số dư API hoặc số lượt check trùng quy đổi. Reader riêng không hiện banner chi phí hay nhãn giá Quét thêm. Chỉ khi kết quả thật dùng API dự phòng mới hiện chi phí lượt quét; số dư API chỉ xuất hiện ở tab Check tài khoản. Backend không gọi API tra số dư cho lượt đọc bằng reader riêng. Giá/quota/ví khách hàng không thay đổi.

Ví header của VIP/khách cũng chỉ hiện ở tab Check tài khoản, gồm số dư và lượt check tài khoản; ẩn ở Kiểm tra kênh và Lịch sử. Thanh ví kênh/lịch sử được ẩn; banner chi phí khách không hiện số dư/lượt check trùng và chỉ hiện khi dùng API dự phòng. Đã test bằng fixture role user/VIP: kênh ẩn → tài khoản hiện 199.870đ / 799 lượt check tài khoản → trở lại kênh ẩn.

Hộp Thông tin tài khoản khách cũng áp dụng cùng quy tắc: mở từ Kiểm tra kênh chỉ còn tên/trạng thái tài khoản và Đổi mật khẩu; ẩn dòng ví và nút nạp. Mở từ Check tài khoản mới hiện ví/lượt check tài khoản và nạp tiền, không còn quy đổi lượt Check trùng.

Bảng quản lý tài khoản, chỉnh số dư, chọn mức nạp QR và thông báo nạp thành công chỉ quy đổi lượt Check tài khoản; bỏ quy đổi lượt Check trùng và các nội dung giá Check trùng trong các màn hình này. Kiểm tra giao diện bảng bằng fixture: 50.000đ → 200 lượt Check TK, 34.500đ → 138 lượt Check TK. Đây là thay đổi hiển thị; không sửa dữ liệu ví/quota/thu phí backend.

Ghi chú cũ kiểu “Quy đổi gói 50k sang Ví chung (đã quét … lượt)” được lọc khỏi bảng quản lý. Nếu chỉ có ghi chú chuyển gói 50k này, hiển thị lại “Tự đăng ký trên web” theo xác nhận chủ web (voitenlua1, cutinhaque01). Giữ các ghi chú khác; không sửa dữ liệu ví.

### Hạn VIP tháng (sửa ngày 25/11/2026)

- Gói PayOS từ 50.000đ kích hoạt VIP 30 ngày (theo cấu hình PayOS), lưu `vip_started_at`, `expires_at`, `vip_source`. Nạp khi gói còn hạn chỉ cộng ví, không kéo dài hạn. Thanh toán sau khi hết hạn bắt đầu kỳ mới.
- Quyền VIP dựa vào vai trò, trạng thái và hạn dùng; số dư ví không tự cấp VIP. Check Trùng của VIP không trừ ví; hết hạn quay về giới hạn miễn phí. Ví Check Tài Khoản vẫn giữ nguyên.
- Khôi phục tài khoản bị mất hạn bằng ngày `paid_at` của đơn PAID, không cộng ví lần nữa và không tạo lại người dùng đã xóa. Admin và VIP vĩnh viễn cấp thủ công được giữ nguyên. Bảng quản lý hiển thị hạn thực tế theo giờ Việt Nam.
- Đã khôi phục `voitenlua1`, `virgo0805`, `ducson2222` theo đơn thanh toán. Theo xác nhận chủ web, `cutinhaque01` và `123456` bắt đầu từ 00:00 ngày 24/11/2026, hết hạn 00:00 ngày 24/12/2026. Bản sao dữ liệu trước sửa nằm trong `independent_reader/.private/backups/` (không đưa vào Git).
- Kiểm tra hồi quy: `python -m unittest test_vip_subscription test_reader_provider -q`.
- Nút Tài Khoản VIP trên đầu trang hiển thị số ngày còn lại và ngày hết hạn (giờ Việt Nam), cập nhật mỗi phút ngay trên trình duyệt. Còn dưới 24 giờ ghi “Còn dưới 1 ngày”; VIP không hạn ghi “Vĩnh viễn”. Thanh đầu trang cho xuống dòng trên điện thoại để không cắt mất hạn dùng. Đã kiểm tra trên trình duyệt bằng dữ liệu giả, gồm gói tháng, dưới 1 ngày, hết hạn và vĩnh viễn.

Đăng nhập Admin → Quản Lý Tài Khoản → Phiên TikTok. Có trạng thái reader, lần đọc thành công/nạp phiên, kết nối MuMu, lịch sử nguồn đọc và số lượt dự phòng; nút kiểm tra và nạp phiên. Luôn ưu tiên reader và luôn fallback API khi reader lỗi; không còn checkbox hay nút lưu chế độ. Cấu hình tắt cũ được bỏ qua. Khung tạo/cập nhật tài khoản VIP đã được bỏ khỏi giao diện, bảng quản lý và thao tác trên tài khoản hiện có vẫn giữ.

Reader tốt → lấy aweme_list/group_id/cờ FYP trực tiếp, không gọi TikHub cho dữ liệu check. Reader lỗi/thiếu group_id/không kết nối được → dùng engine API cũ, có cảnh báo riêng cho Admin. Nếu API cũng lỗi, kết quả vẫn lỗi và luồng ví cũ không thu phí thành công giả.

Reader riêng nhận username/link kênh. Link một video, link rút gọn và ID video giữ engine cũ để không biến yêu cầu kiểm tra một video thành quét cả kênh. Các lượt này được ghi rõ nguồn API dự phòng, không gán nhầm phiên hỏng.

Một kênh lỗi/riêng tư không được kết luận là phiên bị đình chỉ. Thông tin không có cờ FYP không bảo đảm video chắc chắn được đề xuất. Bulk Check Tài Khoản (account_labels), Studio, thanh toán và giá khách hàng không đổi.

## Khi đăng nhập lại trong MuMu

1. Chủ máy mở MuMu, tự đăng nhập TikTok và hoàn tất CAPTCHA/xác minh.
2. Watcher đọc phiên khoảng mỗi 30 giây, tự kết nối lại ADB nếu MuMu đã tắt/mở lại. Không thao tác màn hình hay lấy mật khẩu/OTP.
3. So dấu nhận diện session. Phiên mới phải đọc thử được video có group_id trước khi thay file đang dùng.
4. Thay state nguyên tử, báo xanh. Lượt web kế tiếp lại ưu tiên reader riêng, kể cả vừa dùng API dự phòng.

Nếu cần gấp, bấm Nạp phiên mới từ MuMu. Nếu candidate chưa đọc được thì giữ phiên cũ và báo trạng thái, không tự tuyên bố đổi thành công. Tắt MuMu vẫn dùng phiên đã lưu; PC tắt thì Render dùng API cũ dự phòng. Cấu hình chế độ riêng trên từng runtime, không chuyển session/khóa MuMu lên cloud.

## File và cấu hình

- `dedup_provider.py`: adapter schema web, router nguồn đọc, circuit 30 giây, settings/history đã lọc và giám sát sidecar PC.
- `independent_reader/`: source reader/signer, resolver Chrome và watcher.
- `reader_settings.json`: cấu hình cũ không còn tác dụng; `reader_runtime.json`: lịch sử nguồn đọc đã lọc, ignored và không sync peer.
- `independent_reader/.private/`: session/khóa riêng, loại khỏi Git/Docker; tuyệt đối không đưa vào zip source.
- Biến tùy chọn: `INDEPENDENT_READER_DIR`, `INDEPENDENT_READER_URL`, `INDEPENDENT_READER_KEY_FILE`, `INDEPENDENT_READER_AUTOSTART=0` để tắt tự khởi động. Mặc định dùng reader bundled tại localhost:8767.
- Admin APIs: GET `/api/vip/admin/reader-status`; POST `/reader-settings`, `/reader-refresh` cùng prefix admin. Tất cả yêu cầu Admin thật từ token hiện có.

Source reader dựa trên MIT signer; LICENSE giữ nguyên trong vendor. Không có Manager native/module/cache/license trong reader mới. Engine cũ còn nguyên làm fallback; không sửa bỏ các luồng Studio cũ.

## Kiểm chứng

Sửa thumbnail: mobile response có HEIC đứng trước JPEG; ảnh HEIC tải được nhưng không hiển thị trong trình duyệt đang dùng. Adapter nay ưu tiên URL JPEG/PNG/WebP từ toàn bộ cover/origin_cover/dynamic_cover, giữ nguyên chữ ký URL. Đã xác nhận 18/18 URL JPEG và proxy trả image/jpeg thật cho ảnh mẫu. Kết quả lịch sử lưu URL HEIC cũ cần quét lại kênh để lấy URL mới; không xóa lịch sử khách.

13 test router/watcher/thumbnail bằng fixture, không tiêu tốn API: reader tốt không gọi fallback, fallback một lần khi lỗi, reader/fallback luôn bật bất kể cấu hình cũ, circuit phục hồi khi session khỏe, candidate xấu giữ file cũ, candidate tốt đổi file, MuMu offline giữ phiên, lỗi mục tiêu không gán nhầm phiên hết hạn, link video giữ đúng phạm vi một video, chọn đúng URL ảnh trình duyệt đọc được và giữ nguyên chữ ký.

Live adapter và route `/api/check` với lớp xác thực/dữ liệu ví fixture: seitezhm99d → 18 video, 5 trùng, 2 cờ hạn chế, source independent, 0 API trả phí và không chạy hàm fallback. MuMu đang tắt ở lượt kiểm tra này. Admin status: không token 403; fixture Admin 200, không lộ cookie/token. Chưa cố ý làm hỏng tài khoản/session thật để thử đình chỉ; việc chuyển phiên lỗi được kiểm chứng bằng fixture.

Đã kiểm tra thêm trên worker PC đang chạy bằng xác thực Admin thật: `/api/check` trả đúng 18/5/2, source independent, `admin_billing.api_calls=0`; status báo healthy trong lúc MuMu tắt. HTML mới và bảo vệ Admin 403 đã xác nhận qua domain công khai. Giao diện panel được kiểm tra bằng trình duyệt với fixture Admin, gồm trạng thái xanh, dự phòng cam và banner cảnh báo; không đăng nhập Admin giả vào backend thật và không gọi API trả phí trong test fallback.
