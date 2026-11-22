const BACKUP_ORIGIN = "https://tiktok-dedup-web.onrender.com";

export default {
  async fetch(request, env, ctx) {
    const inUrl = new URL(request.url);
    const method = request.method.toUpperCase();
    const hasBody = !["GET", "HEAD"].includes(method);
    const bodyBuffer = hasBody ? await request.arrayBuffer() : null;

    // 1. Khi truy cập qua tên miền chính checktiktok.site -> ưu tiên máy tính PC trước
    if (!inUrl.hostname.endsWith(".workers.dev")) {
      try {
        const primaryReq = new Request(request.url, {
          method,
          headers: request.headers,
          body: bodyBuffer ? bodyBuffer.slice(0) : null,
          redirect: "manual",
        });
        const primaryRes = await fetch(primaryReq);
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
      } catch (err) {}
    }

    // 2. Khi máy tính TẮT (lỗi 530/1033) -> Tự động chuyển sang Render 24/7
    const backupUrl = new URL(inUrl.pathname + inUrl.search, BACKUP_ORIGIN);
    const fwdHeaders = new Headers(request.headers);
    fwdHeaders.delete("host");
    const clientIp = request.headers.get("cf-connecting-ip");
    if (clientIp) fwdHeaders.set("x-forwarded-for", clientIp);

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

  // 3. Tự động gõ nhẹ mỗi 10 phút giữ Server Render thức 24/7
  async scheduled(event, env, ctx) {
    ctx.waitUntil(fetch(`${BACKUP_ORIGIN}/api/tunnel`).catch(() => {}));
  },
};
