'use strict';
// Isolated browser profile; no Manager sessions, CAPTCHA solving or user Chrome.
const path=require('node:path');
const {chromium}=require('playwright');

function profileFromData(data,username){
  const detail=data?.__DEFAULT_SCOPE__?.['webapp.user-detail'];
  const user=detail?.userInfo?.user;
  const stats=detail?.userInfo?.stats;
  if(!user?.secUid){
    const error=new Error('profile_missing');
    error.profileStatus=detail?.statusCode;
    throw error;
  }
  if(String(user.uniqueId).toLowerCase()!==username.toLowerCase())
    throw new Error('profile_identity_mismatch');
  return {username:user.uniqueId,secUid:user.secUid,nickname:user.nickname||'',
    videoCount:stats?.videoCount??null,followers:stats?.followerCount??0,uid:user.id||'',
    avatar:user.avatarLarger||user.avatarMedium||'',privateAccount:!!user.privateAccount,
    source:'public TikTok profile'};
}

function failureCode(error,title=''){
  return Number(error.profileStatus)===10221 || /couldn['’]t find this account|account not found/i.test(title)
    ? 'profile_not_found' : 'profile_unavailable';
}

function failureMessage(error,stage,title='',httpStatus=0){
  const code=Number.isFinite(Number(error.profileStatus)) ? ` (TikTok statusCode=${Number(error.profileStatus)})` : '';
  if(failureCode(error,title)==='profile_not_found')
    return 'Kênh không tồn tại hoặc không khả dụng trên TikTok'+code+'. Đây là lỗi kênh, không phải lỗi reader. Vui lòng kiểm tra lại username.';
  if(/captcha|security check|verify to continue/i.test(title))
    return 'Trang hồ sơ TikTok đang yêu cầu xác minh trình duyệt. Chưa đọc được hồ sơ kênh.';
  if(stage==='launch')return 'Không mở được Chrome đọc hồ sơ kênh. Cần kiểm tra tiến trình/profile Chrome của reader.';
  if(error.name==='TimeoutError')return stage==='navigation'
    ? 'TikTok tải trang hồ sơ quá thời gian chờ (30 giây).'
    : 'Trang TikTok chưa trả dữ liệu hồ sơ sau 15 giây; chưa xác định là lỗi mạng hay trang bị giới hạn.';
  if(httpStatus>=400)return `Trang hồ sơ TikTok trả HTTP ${httpStatus}; không đọc được hồ sơ kênh.`;
  if(error.message==='profile_identity_mismatch')return 'TikTok trả hồ sơ khác username yêu cầu; reader không dùng nhầm hồ sơ này.';
  if(error.message==='profile_missing')return 'TikTok không trả hồ sơ người dùng'+code+'; chưa có đủ dữ liệu để xác định nguyên nhân.';
  if(error instanceof SyntaxError)return 'TikTok trả dữ liệu hồ sơ không hợp lệ, không đọc được JSON.';
  return 'Không đọc được trang hồ sơ TikTok (giai đoạn '+stage+', '+(error.name||'Error')+').';
}

async function run(username){
  if(!/^[A-Za-z0-9._]{1,64}$/.test(username)){
    process.stdout.write(JSON.stringify({error:'Username không hợp lệ.'}));process.exitCode=2;return;
  }
  let context,page,stage='launch',httpStatus=0;
  try{
    context=await chromium.launchPersistentContext(path.join(__dirname,'.private','public-browser'),
      {channel:'chrome',headless:true,locale:'en-US',timezoneId:'Asia/Ho_Chi_Minh',viewport:{width:1280,height:900}});
    page=await context.newPage();stage='navigation';
    const response=await page.goto('https://www.tiktok.com/@'+encodeURIComponent(username),{waitUntil:'domcontentloaded',timeout:30000});
    httpStatus=response?.status()||0;stage='profile_data';
    const script=page.locator('#__UNIVERSAL_DATA_FOR_REHYDRATION__');
    await script.waitFor({state:'attached',timeout:15000});
    const result=profileFromData(JSON.parse(await script.textContent()),username);
    process.stdout.write(JSON.stringify(result));
  }catch(error){
    const title=page ? await page.title().catch(()=>'') : '';
    process.stdout.write(JSON.stringify({code:failureCode(error,title),error:failureMessage(error,stage,title,httpStatus)}));process.exitCode=2;
  }finally{await context?.close().catch(()=>{});}
}

module.exports={profileFromData,failureMessage,failureCode};
if(require.main===module)run(process.argv[2]||'');
