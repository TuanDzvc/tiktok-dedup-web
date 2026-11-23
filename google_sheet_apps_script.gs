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
  'Vị trí IP',
  'Link kênh',
  'Số lần check',
  'Check gần nhất',
  'Mã máy'
];

function sanitizeSheetName(raw) {
  var name = String(raw || '').trim();
  // Loại bỏ các ký tự bị Google Sheets cấm trong tên trang tính: \ / ? * [ ] :
  name = name.replace(/[\\\/\?\*\[\]:]/g, '_');
  if (!name) name = 'Khách_Free';
  if (name.length > 95) name = name.slice(0, 95);
  return name;
}

function applyColumnWidths(sheet) {
  var widths = [155, 175, 130, 215, 285, 110, 155, 125];
  for (var c = 0; c < widths.length; c++) {
    sheet.setColumnWidth(c + 1, widths[c]);
  }
}

function styleHeaderAndAlignSheet(sheet, migratedFrom7) {
  var headerRange = sheet.getRange(1, 1, 1, SHEET_HEADERS.length);
  headerRange.setValues([SHEET_HEADERS]);
  headerRange.setFontWeight('bold');
  headerRange.setBackground('#0f172a');
  headerRange.setFontColor('#ffffff');
  headerRange.setHorizontalAlignment('center');
  headerRange.setVerticalAlignment('middle');
  sheet.setFrozenRows(1);
  applyColumnWidths(sheet);

  var lastRow = sheet.getLastRow();
  if (lastRow >= 2) {
    var dataRange = sheet.getRange(2, 1, lastRow - 1, SHEET_HEADERS.length);
    dataRange.setHorizontalAlignment('center');
    dataRange.setVerticalAlignment('middle');
  }

  var existingFilter = sheet.getFilter();
  if (existingFilter) {
    existingFilter.remove();
  }
}

function getOrCreateUserSheet(ss, sheetName) {
  var safeName = sanitizeSheetName(sheetName);
  var sheet = ss.getSheetByName(safeName);
  if (!sheet && safeName === 'Khách_Free') {
    var oldGuest = ss.getSheetByName('Khach_Free');
    if (oldGuest) {
      oldGuest.setName('Khách_Free');
      sheet = oldGuest;
    }
  }

  var migratedFrom7 = false;
  if (!sheet) {
    sheet = ss.insertSheet(safeName);
  } else if (sheet.getLastRow() >= 1) {
    var col4Val = String(sheet.getRange(1, 4).getValue() || '').trim();
    if (col4Val === 'Link kênh') {
      // Chèn cột Vị trí IP vào vị trí cột 4 để không làm mất hoặc lệch dữ liệu 7 cột cũ
      sheet.insertColumnAfter(3);
      migratedFrom7 = true;
    }
  }

  styleHeaderAndAlignSheet(sheet, migratedFrom7);
  return { sheet: sheet, name: safeName };
}

function doGet(e) {
  return ContentService
    .createTextOutput('Google Sheet Webhook đang hoạt động tốt (Chế độ tách trang tính theo User & Vị trí IP)!')
    .setMimeType(ContentService.MimeType.TEXT);
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(15000);
  try {
    var raw = (e && e.postData && e.postData.contents) ? e.postData.contents : '{}';
    var data = JSON.parse(raw);
    var action = String(data.action || 'log_check').trim();
    var targetAcc = data.sheet_name || data.account || 'Khách_Free';

    var ss = SpreadsheetApp.getActiveSpreadsheet();
    var res = getOrCreateUserSheet(ss, targetAcc);
    var sheet = res.sheet;

    // 1. Lệnh tạo sẵn / cập nhật định dạng trang tính khi có tài khoản mới hoặc Admin bấm đồng bộ
    if (action === 'init_user_sheet') {
      return ContentService
        .createTextOutput(JSON.stringify({ ok: true, action: 'init_user_sheet', sheet: res.name }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    // 2. Ghi lịch sử check kênh vào đúng trang tính mang tên tài khoản (chia riêng theo từng cặp Kênh + IP)
    var rawUser = String(data.user || '').trim().replace(/^@+/, '');
    if (!rawUser) {
      return ContentService
        .createTextOutput(JSON.stringify({ ok: false, error: 'Missing user' }))
        .setMimeType(ContentService.MimeType.JSON);
    }

    var handle = '@' + rawUser;
    var ip = String(data.ip || '').trim();
    var ipLoc = String(data.ip_location || '').trim() || '—';
    var devId = String(data.device_id || '').trim();
    var nowStr = String(data.time || Utilities.formatDate(new Date(), 'Asia/Ho_Chi_Minh', 'dd/MM/yyyy HH:mm:ss'));
    var link = 'https://www.tiktok.com/' + handle;

    var lastRow = sheet.getLastRow();
    var matchedRow = -1;
    if (lastRow >= 2) {
      var rowsBC = sheet.getRange(2, 2, lastRow - 1, 2).getValues();
      var lowerHandle = handle.toLowerCase();
      for (var i = 0; i < rowsBC.length; i++) {
        var rowHandle = String(rowsBC[i][0] || '').trim().toLowerCase();
        var rowIp = String(rowsBC[i][1] || '').trim();
        if (rowHandle === lowerHandle && rowIp === ip) {
          matchedRow = i + 2;
          break;
        }
      }
    }

    if (matchedRow > 0) {
      var prevCount = Number(sheet.getRange(matchedRow, 6).getValue() || 0);
      sheet.getRange(matchedRow, 3).setValue(ip);
      if (ipLoc && ipLoc !== '—') sheet.getRange(matchedRow, 4).setValue(ipLoc);
      sheet.getRange(matchedRow, 5).setValue(link);
      sheet.getRange(matchedRow, 6).setValue(prevCount + 1);
      sheet.getRange(matchedRow, 7).setValue(nowStr);
      if (devId) sheet.getRange(matchedRow, 8).setValue(devId);
      sheet.getRange(matchedRow, 1, 1, SHEET_HEADERS.length)
        .setHorizontalAlignment('center')
        .setVerticalAlignment('middle');
    } else {
      sheet.appendRow([nowStr, handle, ip, ipLoc, link, 1, nowStr, devId]);
      var newRowIdx = sheet.getLastRow();
      sheet.getRange(newRowIdx, 1, 1, SHEET_HEADERS.length)
        .setHorizontalAlignment('center')
        .setVerticalAlignment('middle');
    }

    return ContentService
      .createTextOutput(JSON.stringify({ ok: true, sheet: res.name, user: handle, ip: ip }))
      .setMimeType(ContentService.MimeType.JSON);
  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ ok: false, error: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  } finally {
    lock.releaseLock();
  }
}
