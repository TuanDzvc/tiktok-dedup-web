'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {profileFromData,failureMessage,failureCode}=require('./resolve_public.cjs');

test('returns only the requested identity and public profile fields',()=>{
  const result=profileFromData({__DEFAULT_SCOPE__:{'webapp.user-detail':{userInfo:{
    user:{uniqueId:'Fixture',secUid:'synthetic-sec',id:'1'},stats:{followerCount:3,videoCount:2}}}}},'fixture');
  assert.equal(result.secUid,'synthetic-sec');assert.equal(result.followers,3);
});
test('missing user preserves the observed TikTok status without inventing a cause',()=>{
  let error;
  try{profileFromData({__DEFAULT_SCOPE__:{'webapp.user-detail':{statusCode:10221}}},'fixture');}catch(e){error=e;}
  assert.equal(error.profileStatus,10221);
  const message=failureMessage(error,'profile_data',"Couldn’t find this account. Visit TikTok");
  assert.match(message,/Kênh không tồn tại hoặc không khả dụng/);assert.match(message,/10221/);
  assert.equal(failureCode(error),'profile_not_found');
  assert.match(message,/không phải lỗi reader/);
  assert.doesNotMatch(message,/cần xác minh/);
});
test('unknown status is not automatically called CAPTCHA or account deletion',()=>{
  const error=Object.assign(new Error('profile_missing'),{profileStatus:123456});
  const message=failureMessage(error,'profile_data','TikTok');
  assert.match(message,/123456/);assert.match(message,/chưa có đủ dữ liệu/);
  assert.doesNotMatch(message,/yêu cầu xác minh|bị xóa/);
  assert.equal(failureCode(error),'profile_unavailable');
});
test('different identity is rejected',()=>{
  assert.throws(()=>profileFromData({__DEFAULT_SCOPE__:{'webapp.user-detail':{
    userInfo:{user:{uniqueId:'other',secUid:'synthetic'}}}}},'fixture'),/profile_identity_mismatch/);
});
test('navigation timeout and missing profile script are distinguishable',()=>{
  const error=Object.assign(new Error(),{name:'TimeoutError'});
  assert.match(failureMessage(error,'navigation'),/30 giây/);
  assert.match(failureMessage(error,'profile_data'),/15 giây/);
});
test('verification only reported when visible title says so',()=>{
  assert.match(failureMessage(new Error(),'profile_data','Security Check'),/yêu cầu xác minh/);
  assert.doesNotMatch(failureMessage(new Error(),'profile_data','TikTok'),/yêu cầu xác minh/);
});
test('Chrome launch and HTTP errors have distinct explanations',()=>{
  assert.match(failureMessage(new Error(),'launch'),/Không mở được Chrome/);
  assert.match(failureMessage(new Error(),'profile_data','TikTok',429),/HTTP 429/);
});
