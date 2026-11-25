# Bộ đọc Check trùng không dùng TikHub

`checkdup.cjs` và `native-reader.cjs` được tách/tái dựng từ luồng Check trùng
của TikTok Manager 3.4.0, dựa trên bytecode gốc SHA-256:
`2b16feeb358255ffc9e6411af568d76154ccbe2d9e0c1eb82760eff384ac846e`.

Luồng chính: chuẩn hoá username → search reader để tìm secUid (dự phòng SSR)
→ đọc `/aweme/v1/aweme/post/` → trả dữ liệu mobile gốc cho bộ phân tích web.
`entry.cjs` là adapter mới, chỉ nhận JSON qua stdin và trả kết quả qua stdout.

Source Rust của bộ ký chưa được khôi phục. Module `hwid_native.node` nguyên bản
được nạp từ bản TikTok Manager đã cài trên PC; module này không nằm trong repo.
Giữ nguyên xác thực chữ ký giấy phép, hạn dùng và ràng buộc HWID. Không sao chép
giấy phép/cookie/token vào source, cloud hoặc snapshot đồng bộ.

## Cấu hình

| Biến | Mặc định / tác dụng |
| --- | --- |
| `DEDUP_PROVIDER` | `native`; đặt `tikhub` chỉ khi chủ động muốn dùng lại engine trả phí |
| `TIKTOK_NATIVE_NODE` | Tự tìm `node.exe` trên PATH của PC |
| `TIKTOK_NATIVE_MODULE` | `%LOCALAPPDATA%/Programs/tiktokmanager/resources/hwid_native.node` |
| `TIKTOK_MANAGER_DATA_DIR` | `%APPDATA%/tiktokmanager` (đọc `license/cache.json`) |
| `DEDUP_NATIVE_BRIDGE_URL` | Render gọi PC qua HTTPS; mặc định `PRIMARY_SYNC_BASE` hoặc `https://checktiktok.site` |
| `DEDUP_NATIVE_BRIDGE_TOKEN` | Tuỳ chọn; nếu không đặt thì suy ra bằng HMAC từ `VIP_AUTH_SECRET` chung giữa PC/Render |

PC Windows luôn gọi reader local, kể cả khi có cấu hình bridge, để tránh vòng lặp.
Linux/Render gọi `/api/internal/native-dedup` trên PC, có xác thực header riêng.
Endpoint từ chối chạy trên cloud; nếu failover quay request về Render thì trả 503,
không gọi lại bridge. Khi PC tắt/giấy phép hết hạn/reader hỏng, Check trùng báo lỗi,
không âm thầm gọi TikHub và không báo kênh sạch do thiếu dữ liệu.

Mỗi process xếp request reader tuần tự, cache kết quả 30 giây (tối đa 128 trang).
Đây là cache RAM, không được đưa vào peer sync hay Supabase.

## Hành vi được giữ

- `/api/check` vẫn dùng 18 bài/trang và opaque mobile cursor khi tải tiếp.
- Giữ bộ phân tích/định dạng kết quả hiện có, cờ FYP, group ID và link video gốc.
- Giữ quyền `tuandzvcc`, quota miễn phí, lịch sử, giá ví 26đ và quy trình PayOS.
- Bỏ bước TikHub bổ sung group ID và các lượt đọc metadata phụ trong nhánh native.
- Cookie chính chủ do người dùng cung cấp vẫn có thể bổ sung FYP/CRP bằng API
  native hiện có; đây là lời gọi TikTok trực tiếp, không phải TikHub.
- Check tài khoản vẫn dùng engine TikHub riêng như trước.

## Kiểm thử

```powershell
python -m unittest discover -s tests -p test_native_dedup.py -v
```

Test chặn mọi hàm TikHub trong nhánh native, kiểm tra schema, FYP, private video,
phân trang, cache, lỗi không fallback trả phí, xác thực bridge và chặn relay loop.

Không cần mở app desktop Check Trùng hoặc TikTok Manager cùng lúc. Node và
module native/giấy phép hợp lệ trên PC vẫn là phụ thuộc bắt buộc.
