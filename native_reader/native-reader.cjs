'use strict';
const fs = require('node:fs');
const path = require('node:path');
function initialize() {
  const appData = process.env.APPDATA;
  const cachePath = path.join(process.env.TIKTOK_MANAGER_DATA_DIR || path.join(appData || '', 'tiktokmanager'), 'license', 'cache.json');
  if (!fs.existsSync(cachePath)) throw new Error('Chưa có giấy phép TikTok Manager trên máy. Mở app gốc để kích hoạt trước.');
  const data = JSON.parse(fs.readFileSync(cachePath, 'utf8'));
  if (!data.activated || !data.cachedAt || Date.now() - data.cachedAt > 3 * 86400000)
    throw new Error('Giấy phép lưu trên máy cần làm mới. Mở TikTok Manager rồi thử lại.');
  if (!data.expiresAt || Date.now() >= Date.parse(String(data.expiresAt).replace(' ', 'T') + 'Z'))
    throw new Error('Giấy phép TikTok Manager đã hết hạn. Làm mới giấy phép trong app gốc.');
  const bundled = path.join(__dirname, '..', 'vendor', 'hwid_native.node');
  const installed = path.join(process.env.LOCALAPPDATA, 'Programs', 'tiktokmanager', 'resources', 'hwid_native.node');
  const native = require(process.env.TIKTOK_NATIVE_MODULE || (fs.existsSync(bundled) ? bundled : installed));
  const payload = [data.hwid || '', data.tool || 'tiktok-manager', data.activated ? 1 : 0,
    data.isTrial ? 1 : 0, data.expiresAt || '', data.issuedAt || ''].join(':');
  const valid = (data.lic_sig2 && native.verifyLicenseV2(payload, data.lic_sig2)) ||
    (data.lic_sig && native.verifyLicense(payload, data.lic_sig));
  if (!valid || !native.arm(payload, data.lic_sig || '', data.lic_sig2 || ''))
    throw new Error('Giấy phép không hợp lệ cho máy này hoặc bộ đọc chưa được khởi tạo.');
  const reader = JSON.parse(native.getCheckpostReader());
  if (!reader?.cookie) throw new Error('Reader của app gốc không khả dụng.');
  return { native, reader };
}
module.exports = { initialize };
