'use strict';
// Product runtime: isolated browser profile; does not use user Chrome or Manager sessions.
const path=require('node:path');
const { chromium }=require('playwright');
const username=process.argv[2] || '';
if(!/^[A-Za-z0-9._]{1,64}$/.test(username))process.exit(2);
(async()=>{
  let context;
  try {
    context=await chromium.launchPersistentContext(path.join(__dirname,'.private','public-browser'),
      {channel:'chrome',headless:true,locale:'en-US',timezoneId:'Asia/Ho_Chi_Minh',viewport:{width:1280,height:900}});
    const page=await context.newPage();
    await page.goto('https://www.tiktok.com/@'+encodeURIComponent(username),{waitUntil:'domcontentloaded',timeout:30000});
    const script=page.locator('#__UNIVERSAL_DATA_FOR_REHYDRATION__');
    await script.waitFor({state:'attached',timeout:15000});
    const data=JSON.parse(await script.textContent());
    const detail=data.__DEFAULT_SCOPE__?.['webapp.user-detail'];
    const user=detail?.userInfo?.user;
    const stats=detail?.userInfo?.stats;
    if(!user?.secUid || String(user.uniqueId).toLowerCase()!==username.toLowerCase())
      throw new Error('Public profile not available; browser verification may be required');
    process.stdout.write(JSON.stringify({username:user.uniqueId,secUid:user.secUid,
      nickname:user.nickname || '',videoCount:stats?.videoCount ?? null,followers:stats?.followerCount??0,
      uid:user.id || '',avatar:user.avatarLarger || user.avatarMedium || '',privateAccount:!!user.privateAccount,
      source:'public TikTok profile'}));
  }catch(e){process.stdout.write(JSON.stringify({error:'Không đọc được hồ sơ công khai; có thể cần xác minh trình duyệt.'}));process.exitCode=2}
  finally {await context?.close()}
})();
