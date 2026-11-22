/**
 * Google Apps Script - Tự động tách mỗi tài khoản đăng ký web thành 1 Trang tính (Sheet Tab) riêng
 * Spreadsheet ID: 1HRGpokdDkzyHFCvJBDishlkR7HaVp6BgyBJPxNCW2A0
 *
 * Hướng dẫn cập nhật (chỉ làm 1 lần):
 * 1. Mở Google Sheet -> Menu [Tiện ích mở rộng] -> [Apps Script].
 * 2. Xóa toàn bộ code cũ và dán toàn bộ đoạn code này vào -> bấm Lưu (Ctrl+S).
 * 3. Bấm [Triển khai] -> [Quản lý các bản triển khai] -> bấm biểu tượng Bút chì (Chỉnh sửa)
 *    -> chọn Phiên bản: [Phiên bản mới] -> bấm [Triển khai] là xong (giữ nguyên URL Webhook cũ).
 */

var SHEET_HEADERS = [
  'Thời gian đầu',
  'Kênh TikTok',
  'IP truy cập',
  'Link kênh',
  'Số lần check',
  'Check gần nhất',
  'Mã máy'
];

function sanitizeSheetName(raw) {
  var name = String(raw || '').trim();
  // Loại bỏ các ký tự bị Google Sheets cấm trong tên trang tính: \ / ? * [ ] :
  name = name.replace(/[\\\/\?\*\[\]:]/g, '_');
  if (!name) name = 'Khach_Free';
  if (name.length > 95) name = name.slice(0, 95);
  return name;
}

function getOrCreateUserSheet(ss, sheetName) {
  var safeName = sanitizeSheetName(sheetName);
  var sheet = ss.getSheetByName(safeName);
  if (!sheet) {
    sheet = ss.insertSheet(safeName);
    var headerRange = sheet.getRange(1, 1, 1, SHEET_HEADERS.length);
    headerRange.setValues([SHEET_HEADERS]);
    headerRange.setFontWeight('bold');
    headerRange.setBackground('#0f172a');
    headerRange.setFontColor('#38bdf8');
    headerRange.setHorizontalAlignment('center');
    sheet.setFrozenRows(1);
    sheet.setColumnWidth(1, 155);
    sheet.setColumnWidth(2, 190);
    sheet.setColumnWidth(3, 135);
    sheet.setColumnWidth(4, 285);
    sheet.setColumnWidth(5, 110);
    sheet.setColumnWidth(6, 155);
    sheet.setColumnWidth(7, 130);
  } else if (sheet.getLastRow() === 0) {
    var emptyHdr = sheet.getRange(1, 1, 1, SHEET_HEADERS.length);
    emptyHdr.setValues([SHEET_HEADERS]);
    emptyHdr.setFontWeight('bold');
    emptyHdr.setBackground('#0f172a');
    emptyHdr.setFontColor('#38bdf8');
    sheet.setFrozenRows(1);
  }
  return { sheet: sheet, name: safeName };
}

function doGet(e) {
  return ContentService
    .createTextOutput('Google Sheet Webhook đang hoạt động tốt (Chế độ tách trang tính theo User)!')
    .setMimeType(ContentService.MimeType.TEXT);
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(15000);
  try {
    var raw = (e && e.postData && e.postData.contents) ? e.postData.contents : '{}';
    var data = JSON.parse(raw);
    var action = String(data.action || 'log_check').trim();
    var targetAcc = data.sheet_name || data.account || 'Khach_Free';

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var res = getOrCreateUserSheet(ss, targetAcc);
    var sheet = res.sheet;

    // 1. Lệnh tạo sẵn trang tính khi có tài khoản mới đăng ký hoặc Admin bấm đồng bộ
    if (action === 'init_user_sheet') {
      return ContentService
        .createTextOutput(JSON.stringify({ ok: true, action: 'init_user_sheet', sheet: res.name }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    // 2. Ghi lịch sử check kênh vào đúng trang tính mang tên tài khoản đã đăng nhập
    var rawUser = String(data.user || '').trim().replace(/^@+/, '');
    if (!rawUser) {
      return ContentService
        .createTextOutput(JSON.stringify({ ok: false, error: 'Missing user' }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    var handle = '@' + rawUser;
    var ip = String(data.ip || '').trim();
    var devId = String(data.device_id || '').trim();
    var nowStr = String(data.time || Utilities.formatDate(new Date(), 'Asia/Ho_Chi_Minh', 'dd/MM/yyyy HH:mm:ss'));
    var link = 'https://www.tiktok.com/' + handle;

    var lastRow = sheet.getLastRow();
    var matchedRow = -1;
    if (lastRow >= 2) {
      var colB = sheet.getRange(2, 2, lastRow - 1, 1).getValues();
      var lowerHandle = handle.toLowerCase();
      for (var i = 0; i < colB.length; i++) {
        if (String(colB[i][0] || '').trim().toLowerCase() === lowerHandle) {
          matchedRow = i + 2;
          break;
        }
      }
    }

    if (matchedRow > 0) {
      var prevCount = Number(sheet.getRange(matchedRow, 5).getValue() || 0);
      sheet.getRange(matchedRow, 3).setValue(ip);
      sheet.getRange(matchedRow, 4).setValue(link);
      sheet.getRange(matchedRow, 5).setValue(prevCount + 1);
      sheet.getRange(matchedRow, 6).setValue(nowStr);
      if (devId) sheet.getRange(matchedRow, 7).setValue(devId);
    } else {
      sheet.appendRow([nowStr, handle, ip, link, 1, nowStr, devId]);
    }

    return ContentService
      .createTextOutput(JSON.stringify({ ok: true, sheet: res.name, user: handle }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ ok: false, error: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  } finally {
    lock.releaseLock();
  }
}
