/**
 * Cloudflare Worker: Tự động chuyển đổi dự phòng 24/7 cho checktiktok.site
 * - Khi máy tính (PC) ĐANG BẬT: Chạy thẳng qua Cloudflare Tunnel về PC (siêu tốc 1.5s, lưu ổ cứng vĩnh viễn).
 * - Khi máy tính (PC) TẮT (mã 530 / 502 / 503 / 504 / >=520): Tự động chuyển sang Server dự phòng Render 24/7!
 * - Tích hợp Cron giữ ấm Server Render mỗi 10 phút để không bao giờ bị ngủ đông.
 */

const BACKUP_ORIGIN = "https://tiktok-dedup-web.onrender.com";

export default {
  async fetch(request, env, ctx) {
    const method = request.method.toUpperCase();
    const hasBody = !["GET", "HEAD"].includes(method);
    const bodyBuffer = hasBody ? await request.arrayBuffer() : null;

    // 1. Ưu tiên gọi về máy tính chính (qua Cloudflare Tunnel hiện tại của tên miền)
    try {
      const primaryReq = new Request(request.url, {
        method,
        headers: request.headers,
        body: bodyBuffer ? bodyBuffer.slice(0) : null,
        redirect: "manual",
      });

      const primaryRes = await fetch(primaryReq);

      // Nếu máy tính đang bật (không phải lỗi đứt Tunnel 530 hoặc lỗi cổng 502/503/504/52x) -> trả kết quả ngay!
      const st = primaryRes.status;
      if (st !== 530 && st !== 502 && st !== 503 && st !== 504 && st < 520) {
        const outHeaders = new Headers(primaryRes.headers);
        outHeaders.set("X-Served-By", "PC-Primary");
        return new Response(primaryRes.body, {
          status: primaryRes.status,
          statusText: primaryRes.statusText,
          headers: outHeaders,
        });
      }
    } catch (err) {
      // Máy tính tắt hoặc đứt kết nối -> tự động rơi xuống nhánh Backup bên dưới
    }

    // 2. Máy tính đang TẮT -> Tự động lái sang Server Cloud Render 24/7
    const inUrl = new URL(request.url);
    const backupUrl = new URL(inUrl.pathname + inUrl.search, BACKUP_ORIGIN);

    const fwdHeaders = new Headers(request.headers);
    fwdHeaders.delete("host");
    const clientIp = request.headers.get("cf-connecting-ip");
    if (clientIp) {
      fwdHeaders.set("x-forwarded-for", clientIp);
    }

    const backupReq = new Request(backupUrl.toString(), {
      method,
      headers: fwdHeaders,
      body: bodyBuffer ? bodyBuffer.slice(0) : null,
      redirect: "manual",
    });

    const backupRes = await fetch(backupReq);
    const resHeaders = new Headers(backupRes.headers);
    resHeaders.set("X-Served-By", "Render-Backup-247");

    return new Response(backupRes.body, {
      status: backupRes.status,
      statusText: backupRes.statusText,
      headers: resHeaders,
    });
  },

  // 3. Hẹn giờ tự động gọi nhẹ mỗi 10 phút giữ Server Render luôn thức 24/7
  async scheduled(event, env, ctx) {
    ctx.waitUntil(
      fetch(`${BACKUP_ORIGIN}/api/tunnel`, { method: "GET" }).catch(() => {})
    );
  },
};
