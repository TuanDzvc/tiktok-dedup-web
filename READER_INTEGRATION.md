# Check trùng ưu tiên phiên TikTok, API dự phòng

Ngày triển khai: 25/11/2026. Luồng `/api/check` giữ nguyên xác thực, quota, phí ví khách hàng, phân quyền quét thêm, lịch sử và ảnh. Chỉ thay nguồn dữ liệu: reader riêng trước, engine API cũ làm dự phòng.

Trạng thái triển khai: PC đã cập nhật và domain trả `X-Served-By: PC-Primary`. GitHub từ chối credential đã lưu (401); người dùng chọn cập nhật PC trước, Render làm sau. Commit nằm local; Render vẫn chạy bản API cũ, chưa có bảng quản lý phiên mới khi PC tắt.

## Vận hành trên web

### Số dư API khi F5 / API chậm (26/11/2026)

Đã bỏ cơ chế trả số dư 0 khi thiếu key, timeout, HTTP lỗi hoặc JSON thiếu balance. `api_balance_cache.py` chỉ nhận số dư có trường hợp lệ (kể cả số 0 thật), cộng balance + free_credit nhất quán USD/VNĐ. Giữ bản xác nhận gần nhất trong bộ nhớ và file private tối đa 24h, gắn fingerprint key để không lấy nhầm số dư khi đổi API key. Cache bình thường 15 giây, gộp request đồng thời, có cooldown lỗi; chưa từng có giá trị hợp lệ trả `balance_known=false`, số tiền null và HTTP 503, không giả số 0. Chi phí scan vẫn hiển thị độc lập nếu không tải được số dư.

Giao diện lưu số dư hợp lệ theo username Admin trong sessionStorage để F5 hiện ngay số gần nhất, không ghi đè bằng kết quả lỗi. Có ghi chú trạng thái gần nhất trong tooltip; chưa có dữ liệu thì “Đang tải…”/“Chưa tải được”. Request gộp, timeout 10 giây và thử lại theo backoff 2–30 giây; chỉ đánh dấu đã tải khi thành công. Khắc phục cả trường hợp API có free credit mà số USD trước đây chọn nhầm balance tiền mặt trước total.

53 tests Python qua; kiểm thử trình duyệt mô phỏng lỗi → phục hồi tự động, F5 khi API lỗi vẫn giữ $3.7980 / 96.849đ, 0 thật hiển thị 0, 3 lời gọi đồng thời chỉ phát 1 request. API công khai trên PC đã kiểm tra trả số dư xác nhận hợp lệ. Dữ liệu test giao diện là fixture, không thay số dư thực.

### Chẩn đoán lỗi hồ sơ kênh ngày 26/11/2026

Đã tách nghiệp vụ `profile_not_found` (mã hồ sơ 10221 hoặc tiêu đề TikTok xác nhận không tìm thấy tài khoản): trả “Kênh không tồn tại hoặc không khả dụng … không phải lỗi reader”, không gọi API trả phí, không mở circuit và không đánh dấu phiên lỗi. Nhật ký dùng nguồn `target_unavailable`, màu trung tính, tên kênh và lý do riêng; không tăng bộ đếm fallback. Lỗi hồ sơ chưa rõ nguyên nhân vẫn dùng cơ chế dự phòng như cũ. Kiểm tra thật qua `/api/check` cho kênh trên: `success=false`, mã `profile_not_found`, `fallback=false`, trạng thái reader vẫn healthy, fallback count giữ nguyên. Fixture trình duyệt xác nhận badge xanh, lỗi kênh có nhãn riêng và ẩn cảnh báo reader/API.

Đối chiếu log: `@beverlyzwssg65403` lỗi hồ sơ cả 18:06 và 18:09; các kênh khác vẫn chuyển lại reader thành công. Mở TikTok công khai trực tiếp cho kênh này thấy “Couldn't find this account”, dữ liệu `webapp.user-detail.statusCode=10221`, không có user. Gọi sidecar độc lập cũng nhận thiếu user/mã 10221; kênh đối chứng `seitezhm99d` vẫn đọc được video. Đây là lỗi hồ sơ kênh ở lần thử, không có bằng chứng phiên đọc hết hạn; không suy ra tài khoản bị xóa/đình chỉ hay nguyên nhân của log cũ không có username.

Resolver cũ bắt mọi exception rồi ghi chung “có thể cần xác minh trình duyệt”, nên log không phân biệt được nguyên nhân. `resolve_public.cjs` nay giữ mã hồ sơ và phân biệt lỗi Chrome, timeout điều hướng, thiếu script, HTTP lỗi, JSON lỗi, hồ sơ không khớp username và trang xác minh có bằng chứng từ tiêu đề. Không giải CAPTCHA, không sửa/tạo dữ liệu cho log cũ. Sidecar gọi script mới mỗi lần cần tra hồ sơ nên thay đổi có hiệu lực mà không khởi động lại web/sidecar.

Kiểm tra: `node --test independent_reader/test_resolve_public.cjs` (7 tests); thử thật hai kênh trực tiếp qua sidecar, không dùng paid API. Không thay bộ thanh toán hay các thay đổi quản lý nhóm kênh đang có trong working tree.

### Thanh toán tách riêng từ 26/11/2026 (quy tắc hiện hành)

- **VIP Check Trùng:** 50.000đ / 30 ngày theo cấu hình PayOS. Chỉ cấp/gia hạn VIP, không cộng ví Check tài khoản. Mua khi còn hạn nối thêm 30 ngày từ hạn hiện tại; hết hạn tính từ lúc thanh toán. VIP vĩnh viễn không cần mua thêm.
- **Ví Check Tài Khoản:** nạp riêng từ 50.000đ, cộng toàn bộ khoản nhận vào ví; 250đ/tài khoản thành công, lỗi không trừ. Tài khoản thường dùng được, không cần VIP; nạp ví không cấp/gia hạn VIP.
- Màn QR có hai lựa chọn rõ ràng. Tab Check tài khoản mặc định mở nạp ví; tab kiểm tra kênh mặc định mở VIP. VIP ẩn bộ chọn số tiền; ví có mức nạp tùy chọn. Thông báo, đăng ký và kết quả thanh toán đều phân biệt hai khoản. Xử lý phản hồi cũ khi đổi lựa chọn; mở lại đơn đang chờ giữ đúng đơn và loại thanh toán, không tự tạo đơn mới.
- Đơn mới bắt buộc `order_type` là `vip` hoặc `account`, chụp `billing_policy=split_v1`, số tiền và thời hạn. Không tái dùng đơn cơ chế cũ. Xác nhận thanh toán dùng loại của đơn đã lưu, không tin loại do client gửi. Gọi lại webhook/check-order không cộng/gia hạn lần nữa. Public order có `wallet_credit_vnd`; VIP mới bằng 0, ví mới có `duration_days=0`.
- Giữ nguyên số dư, hạn hiện có và đơn cũ: đơn chưa có `split_v1` tiếp tục được hưởng cơ chế lúc tạo (VIP kèm cộng ví). Không trừ lại tiền hoặc hủy quyền cũ. Backup trước cập nhật tại `independent_reader/.private/backups/split_payments_*`.
- 38 kiểm thử qua (`test_split_payments`, `test_vip_subscription`, `test_usage_counts`, `test_reader_provider`). Kiểm thử giao diện dùng fixture chặn toàn bộ request API: cả hai loại, tài khoản thường nạp ví, kết quả thanh toán, mở lại đơn ví, đổi lựa chọn nhanh, màn hình 390px. Không tạo QR PayOS thật hoặc chuyển tiền để thử.
- **Phạm vi triển khai: PC. Render chưa cập nhật theo lựa chọn trước của người dùng.** Khi PC tắt/chuyển sang Render, bản dự phòng còn cơ chế cũ; không coi thanh toán tách khoản đã được triển khai toàn bộ hai runtime. Cần cập nhật Render cùng phiên bản trước khi dựa vào cơ chế này trên nhánh dự phòng.

Các ghi chú phí/gia hạn ghi trước mục này là lịch sử; quy tắc tách khoản trên áp dụng cho đơn mới.

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

VIP expiry now uses a separate two-line header box (remaining days / expiry date); the account button keeps only its title and username. Browser verification on PC-served checktiktok.site confirmed both boxes have two rows.

### Thống kê lượt sử dụng riêng

Bảng quản lý đổi “Số dư Ví Chung” thành “Số dư check tài khoản”; thay tổng “Lượt quét” bằng “Đã Check trùng” và “Đã Check tài khoản”. Backend lưu riêng `dedup_check_count` và `account_check_count`. Check trùng chỉ cộng khi lượt đầu thành công, không bị khóa kết quả; không cộng lượt quét thêm video hoặc đọc lịch sử. Check tài khoản cộng theo số kết quả thành công, kể cả kết quả cache; tài khoản lỗi không cộng.

`scan_count` cũ chứa cả Check trùng và mọi kết quả Check tài khoản (kể cả lỗi), trong khi `account_check_count` chỉ chứa kết quả thành công. Không thể khôi phục chính xác toàn bộ Check trùng cũ từ hai số này. Khởi tạo một lần bằng `max(0, scan_count - account_check_count)`, đánh dấu `dedup_count_estimated` và hiện “≈” kèm giải thích khi rê chuột. Các lượt mới tăng độc lập, không tiếp tục suy ra từ tổng. Giữ tổng cũ để tương thích đồng bộ, merge bộ đếm riêng theo max. Không sửa ví, hạn dùng hoặc mật khẩu; backup trước chuyển đổi trong thư mục private.

Kiểm tra: 22 tests qua (`test_usage_counts`, `test_vip_subscription`, `test_reader_provider`); API PC công khai trả đủ hai bộ đếm cho 10 user và giữ nguyên số dư/hạn. Fixture giao diện: tổng cũ 65, Check TK 62 → Check trùng ≈3 và Check TK 62 ở hai cột riêng.

Owner header balance exception: admin tuandzvcc sees API USD/VND balance on checker and account_check tabs. Other users keep the previous visibility rules. Fetch the balance at initial render and on entering either allowed tab. Scan-cost banners still require API fallback. Browser fixture verified owner checker/account visible, history hidden, other-admin checker hidden and VIP checker hidden.

Owner balance visibility extended to history as requested: tuandzvcc now sees USD/VND on checker, account_check and history. Other users remain unchanged. Verified the helper served publicly against owner, other-admin and VIP history cases.

### Lý do hạn chế FYP (26/11/2026)

Adapter reader trước đây ghi cứng “Không lên FYP”, làm mất diễn giải và bỏ qua mã lý do. Đã dùng lại bộ định dạng lý do/mã của `core_dedup`, giữ nội dung biên bản khi dữ liệu có `reason_codes`/`penalty_reason_info_list`; đọc thêm penalty context và trạng thái bản quyền/gỡ/cấm phân phối. Nếu người dùng cung cấp cookie chính chủ, gọi enrichment native hiện có cho video bị hạn chế (không qua TikHub và không lấy phiên Manager). Enrichment lỗi không làm chuyển một lượt đọc tốt sang API trả phí.

Đối chiếu trực tiếp reader kênh `bhaijan15781.t`, hai video `7711918759103040781` và `7711916198442585374`: có `is_nff_or_nr=true`, `ec_global_verdict=3`, không có reason code/title cụ thể. Với dữ liệu này chỉ hiển thị đúng lời giải thích cũ “Không đủ điều kiện xuất hiện trên bảng tin Cho bạn (Hạn chế FYP · Cần cookie chính chủ để xem chi tiết lý do)”; không suy mã 13/bản quyền từ cờ NFF hoặc mã lifecycle. Tooltip bảng/lịch sử cũ có chuỗi ngắn cũng được chuẩn hóa khi hiển thị, không sửa dữ liệu lịch sử gốc.

29 tests qua (provider, usage, VIP); kiểm tra dữ liệu thật hai video qua reader riêng, không dùng paid API; fixture trình duyệt trên domain PC xác nhận tooltip lịch sử và mã lý do chi tiết được giữ nguyên. Chưa có cookie chính chủ của kênh thử để xác minh biên bản chi tiết của đúng hai video này.
