param([Parameter(Mandatory=$true)][string]$ConfigPath,[string]$JobPath='')
$ErrorActionPreference='Stop'
[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.Security
Add-Type -AssemblyName System.Web.Extensions
Add-Type -AssemblyName System.Web
Add-Type -AssemblyName System.Net.Http
Add-Type -AssemblyName System.IO.Compression.FileSystem
$workerRoot=Split-Path -Parent $ConfigPath
$helperPath=Join-Path $workerRoot 'mail_cdp_worker.cs'
Add-Type -Path $helperPath -ReferencedAssemblies @('System.dll','System.Core.dll','System.Net.Http.dll','System.Web.Extensions.dll','System.IO.Compression.dll','System.IO.Compression.FileSystem.dll')
function Read-Protected([string]$Path) {
    $bytes=[Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($Path),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
    return ([Text.Encoding]::UTF8.GetString($bytes)|ConvertFrom-Json)
}
function Write-Protected([string]$Path,$Value) {
    $bytes=[Text.Encoding]::UTF8.GetBytes(($Value|ConvertTo-Json -Depth 30 -Compress))
    $encrypted=[Security.Cryptography.ProtectedData]::Protect($bytes,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
    [IO.File]::WriteAllBytes($Path,$encrypted)
}
$config=Read-Protected $ConfigPath
$origin=[Uri]$config.origin
if($origin.Scheme -ne 'https' -and -not $origin.IsLoopback){throw 'HTTPS required'}
function Api([string]$Action,$Body) {
    try {
        return Invoke-RestMethod -UseBasicParsing -Uri ($config.origin+'/api/outlook-oauth/worker/'+$Action) -Method Post -Headers @{'X-Mail-Worker-Token'=$config.token;'X-Mail-Worker-Id'=$config.id} -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes(($Body|ConvertTo-Json -Depth 30 -Compress))) -TimeoutSec 30
    } catch {
        if($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 403){throw (New-Object UnauthorizedAccessException('Worker revoked'))}
        throw
    }
}
function Stop-OwnedProcess($Process) {
    try { if(-not $Process.HasExited){$p=Start-Process -FilePath taskkill.exe -WindowStyle Hidden -ArgumentList @('/PID',[string]$Process.Id,'/T','/F') -PassThru -Wait} } catch {}
}
if(-not $JobPath) {
    $mutex=New-Object Threading.Mutex($false,('Local\TikFlowMailWorker-'+$config.id))
    if(-not $mutex.WaitOne(0)){exit}
    $children=@{}
    [IO.File]::WriteAllText((Join-Path $workerRoot 'worker.pid'),[string]$PID)
    try {
        while(-not (Test-Path -LiteralPath (Join-Path $workerRoot 'stop.flag'))) {
            foreach($pending in @(Get-ChildItem -LiteralPath $workerRoot -Filter 'result-*.bin')){
                try {
                    $resultBody=Read-Protected $pending.FullName
                    $null=Api 'event' $resultBody
                    Remove-Item -LiteralPath $pending.FullName -Force
                } catch {
                    if($_.Exception -is [UnauthorizedAccessException]){throw}
                }
            }
            foreach($id in @($children.Keys)){
                if($children[$id].process.HasExited){Remove-Item -LiteralPath $children[$id].path -Force -ErrorAction SilentlyContinue;$children.Remove($id)}
            }
            try {
                $reply=Api 'poll' @{name=$env:COMPUTERNAME;active=@($children.Keys)}
                foreach($id in @($reply.cancel)) { if($children.ContainsKey($id)){Stop-OwnedProcess $children[$id].process} }
                foreach($job in @($reply.jobs)) {
                    if($null -eq $job -or $children.ContainsKey([string]$job.id)){continue}
                    $payload=@{job=$job;runtime=$reply.runtime}
                    $path=Join-Path $workerRoot ('job-'+$job.id+'.bin')
                    Write-Protected $path $payload
                    $childArguments=@('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+$PSCommandPath+'"'),'-ConfigPath',('"'+$ConfigPath+'"'),'-JobPath',('"'+$path+'"'))
                    $proc=Start-Process powershell.exe -WindowStyle Hidden -ArgumentList $childArguments -PassThru
                    $children[[string]$job.id]=@{process=$proc;path=$path}
                }
            } catch { # Keep authorized jobs alive through temporary network outages.
                if($_.Exception -is [UnauthorizedAccessException]){break}
                [IO.File]::WriteAllText((Join-Path $workerRoot 'connection.log'),('Web connection unavailable at '+[DateTime]::UtcNow.ToString('o')))
            }
            Start-Sleep -Seconds 2
        }
    } finally {foreach($entry in $children.Values){Stop-OwnedProcess $entry.process};$mutex.ReleaseMutex();$mutex.Dispose();Remove-Item -LiteralPath (Join-Path $workerRoot 'worker.pid') -Force -ErrorAction SilentlyContinue}
    exit
}

$payload=Read-Protected $JobPath
$job=$payload.job
$script:lastLog=''
$script:lastHeartbeat=[DateTime]::UtcNow.AddMinutes(-1)
function Event([string]$Kind,[string]$Text='',$Extra=@{}) {
    $body=@{job_id=$job.id;lease=$job.lease;kind=$Kind;text=$Text}
    foreach($key in $Extra.Keys){$body[$key]=$Extra[$key]}
    $result=Api 'event' $body
    if($result.cancel){throw 'Cancelled by web'}
    return $result
}
function Stage([string]$Text) {
    if($Text -ne $script:lastLog){$null=Event 'log' $Text;$script:lastLog=$Text}
}
function Heartbeat {
    if(([DateTime]::UtcNow-$script:lastHeartbeat).TotalSeconds -ge 2){$null=Event 'heartbeat';$script:lastHeartbeat=[DateTime]::UtcNow}
}
function Ensure-Browser {
    $root=Join-Path $env:LOCALAPPDATA ('TikFlowUploader\fingerprint-chromium\runtime\'+$payload.runtime.version+'\browser')
    $lock=New-Object Threading.Mutex($false,'Local\TikFlowMailChromiumInstall148')
    while(-not $lock.WaitOne(1000)){Heartbeat}
    try {
        $exe=Get-ChildItem -LiteralPath $root -Filter chrome.exe -Recurse -ErrorAction SilentlyContinue|Select-Object -First 1
        if($exe){return $exe.FullName}
        Stage 'Downloading pinned Chromium runtime on this client (first run only)'
        $zip=Join-Path $workerRoot 'chromium.zip'
        $client=New-Object Net.WebClient
        try {
            $download=$client.DownloadFileTaskAsync([Uri]$payload.runtime.url,$zip)
            while(-not $download.IsCompleted){Heartbeat;Start-Sleep -Milliseconds 500}
            $null=$download.GetAwaiter().GetResult()
        } finally {$client.Dispose()}
        if((Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower() -ne $payload.runtime.sha256){throw 'Chromium SHA256 mismatch'}
        Stage 'Extracting verified Chromium runtime on this client'
        $staging=$root+'.staging-'+[Guid]::NewGuid().ToString('N')
        New-Item -ItemType Directory -Force -Path $staging|Out-Null
        [IO.Compression.ZipFile]::ExtractToDirectory($zip,$staging)
        if(-not (Get-ChildItem -LiteralPath $staging -Filter chrome.exe -Recurse|Select-Object -First 1)){throw 'Incomplete Chromium archive'}
        Move-Item -LiteralPath $staging -Destination $root
        Remove-Item -LiteralPath $zip -Force
        $exe=Get-ChildItem -LiteralPath $root -Filter chrome.exe -Recurse|Select-Object -First 1
        if(-not $exe){throw 'Chromium executable missing'}
        return $exe.FullName
    } finally {$lock.ReleaseMutex();$lock.Dispose()}
}
function Cdp($Session,[string]$Method,$Parameters=@{}) {
    return $Session.SendAsync($Method,$Parameters,15000).GetAwaiter().GetResult()
}
function Evaluate($Session,[string]$Expression) {
    $text=$Session.EvalJsonAsync($Expression,10000).GetAwaiter().GetResult()
    if($text){return ($text|ConvertFrom-Json)}
    return $null
}
function Probe($Session) {
    return Evaluate $Session @'
JSON.stringify((()=>{const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};const point=e=>{const r=e.getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}};return{url:location.href,body:(document.body?.innerText||'').slice(0,24000),inputs:[...document.querySelectorAll('input')].filter(e=>visible(e)&&!['hidden','submit','button','checkbox'].includes(e.type)).map(e=>({...point(e),type:e.type,id:e.id,name:e.name,label:e.getAttribute('aria-label')||e.placeholder||'',max:e.maxLength})),buttons:[...document.querySelectorAll('button,input[type=submit],input[type=button],a,[role=button],[role=listitem],li')].filter(visible).map(e=>({...point(e),text:(e.innerText||e.value||e.getAttribute('aria-label')||'').trim(),id:e.id}))}})())
'@
}
function Click($Session,$Element) {if($Element){$null=$Session.ClickPointAsync([double]$Element.x,[double]$Element.y).GetAwaiter().GetResult();Start-Sleep -Milliseconds 250}}
function Fill($Session,$Element,[string]$Value) {
    Click $Session $Element
    $null=Cdp $Session 'Input.dispatchKeyEvent' @{type='keyDown';key='a';code='KeyA';windowsVirtualKeyCode=65;modifiers=2}
    $null=Cdp $Session 'Input.dispatchKeyEvent' @{type='keyUp';key='a';code='KeyA';windowsVirtualKeyCode=65;modifiers=2}
    $null=Cdp $Session 'Input.insertText' @{text=$Value}
}
function Button($Probe,[string]$Pattern) {return @($Probe.buttons|Where-Object {$_.text -match $Pattern}|Select-Object -First 1)[0]}
function New-Tab([string]$Base,[string]$Url) {
    $page=Invoke-RestMethod -UseBasicParsing -Method Put -Uri ($Base+'/json/new?'+[Uri]::EscapeDataString($Url))
    $session=New-Object TikFlowMailHelper.CdpSession
    try{$null=$session.ConnectAsync($page.webSocketDebuggerUrl,12000).GetAwaiter().GetResult()}
    catch{$session.Dispose();throw}
    return $session
}
function Start-ClientBrowser([string]$Executable,[string[]]$BrowserArguments) {
    try{return Start-Process -FilePath $Executable -ArgumentList $BrowserArguments -WindowStyle Normal -PassThru}
    catch{
        $errorRecord=$_
        $cause=$_.Exception;$nativeCode=0
        while($cause){if($cause -is [ComponentModel.Win32Exception]){$nativeCode=$cause.NativeErrorCode};$cause=$cause.InnerException}
        $blocked=$nativeCode -in @(577,1260,4551) -or $errorRecord.Exception.Message -match 'Application Control policy|blocked by group policy'
        if(-not $blocked){throw}
        $edgePaths=@((Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),(Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'))
        $edge=$null
        foreach($candidate in $edgePaths){
            if(Test-Path -LiteralPath $candidate){
                $signature=Get-AuthenticodeSignature -LiteralPath $candidate
                if($signature.Status -eq 'Valid' -and $signature.SignerCertificate.Subject -match 'O=Microsoft Corporation'){$edge=$candidate;break}
            }
        }
        if(-not $edge){$script:failureCode='browser_policy_blocked';throw 'Application Control blocked Chromium; no verified Microsoft Edge fallback is installed'}
        Stage 'Windows Application Control blocked Fingerprint Chromium; using verified Microsoft Edge on this client'
        $script:activeBrowser='Microsoft Edge (Application Control fallback)'
        return Start-Process -FilePath $edge -ArgumentList $BrowserArguments -WindowStyle Normal -PassThru
    }
}
function Code-Inputs($Probe) {
    $digits=@($Probe.inputs|Where-Object {$_.max -eq 1 -and $_.type -in @('text','tel','number','')})
    if($digits.Count -eq 6){return $digits}
    return @($Probe.inputs|Where-Object {$_.name -match 'otc|code' -or $_.id -match 'otc|code' -or $_.label -match 'verification code|security code' -or $_.max -eq 6}|Select-Object -First 1)
}
function Is-RecoveryMessage($Record) {
    return [string]$Record.from -match 'account-security-noreply' -and [string]$Record.subject -match 'single[- ]use code|security code|verify your|verification code|mã bảo mật|xác minh'
}
function Extract-RecoveryCode($Payload) {
    $text=if($Payload -is [string]){$Payload}else{$Payload|ConvertTo-Json -Depth 20 -Compress}
    $text=[Net.WebUtility]::HtmlDecode(($text -replace '<[^>]+>',' '))
    $match=[regex]::Match($text,'(?i)(?:your\s+single[- ]use\s+code\s+is|security\s+code(?:\s+is)?|verification\s+code(?:\s+is)?|mã\s+bảo\s+mật)\s*:?\s*(\d{6})(?!\d)')
    if($match.Success){return $match.Groups[1].Value}
    $text=$text -replace '"(?:id|createdAt|timestamp)"\s*:\s*(?:"[^"\\]*"|\d+)',''
    $codes=@([regex]::Matches($text,'(?<!\d)\d{6}(?!\d)')|ForEach-Object {$_.Value}|Select-Object -Unique)
    if($codes.Count -eq 1){return $codes[0]}
    return ''
}
function Submit-RecoveryCode($Session,$Probe,[string]$Code) {
    if($Code -notmatch '^\d{6}$'){return $false}
    $boxes=@(Code-Inputs $Probe)
    if($boxes.Count -eq 6){for($di=0;$di -lt 6;$di++){Fill $Session $boxes[$di] ([string]$Code[$di])}}
    elseif($boxes.Count -eq 1){Fill $Session $boxes[0] $Code}
    else{return $false}
    Click $Session (Button $Probe '^(next|verify|continue|submit|ti.p t.c|ti.p theo|x.c minh)$')
    return $true
}
function Recovery-Item($Session,[string]$Subject) {
    $encoded=$Subject|ConvertTo-Json -Compress
    return Evaluate $Session ("JSON.stringify((()=>{const subject="+$encoded+";const e=[...document.querySelectorAll('[role=listitem],li,button,p,span,div')].find(e=>e.textContent.trim()===subject&&e.getBoundingClientRect().width>0&&e.getBoundingClientRect().height>0);if(!e)return null;const r=(e.closest('[role=listitem],li')||e).getBoundingClientRect();return{x:r.x+r.width/2,y:r.y+r.height/2}})())")
}
$script:recoveryCache=@{}
function Update-RecoveryCache($Session) {
    $key=[string]$Session.GetHashCode()
    if(-not $script:recoveryCache.ContainsKey($key)){$script:recoveryCache[$key]=@{pending=@{};records=@();messages=@{}}}
    $cache=$script:recoveryCache[$key]
    foreach($event in @($Session.DrainEventsJson()|ConvertFrom-Json)){
        $rid=[string]$event.params.requestId
        if($event.method -eq 'Network.responseReceived' -and $event.params.response.url -match '/messages?\?'){
            $cache.pending[$rid]=@{url=[string]$event.params.response.url;finished=$false;at=[DateTime]::UtcNow}
        }elseif($event.method -eq 'Network.loadingFinished' -and $cache.pending.ContainsKey($rid)){$cache.pending[$rid].finished=$true}
        elseif($event.method -eq 'Network.loadingFailed'){$cache.pending.Remove($rid)}
    }
    foreach($rid in @($cache.pending.Keys)){
        $pending=$cache.pending[$rid]
        if(([DateTime]::UtcNow-$pending.at).TotalSeconds -gt 25){$cache.pending.Remove($rid);continue}
        if(-not $pending.finished){continue}
        try{
            $response=Cdp $Session 'Network.getResponseBody' @{requestId=$rid}
            $text=[string]$response['body']
            if($response['base64Encoded']){$text=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($text))}
            $data=$text|ConvertFrom-Json
            if($pending.url -match '/messages\?'){$cache.records=@($data.result);$cache.recordsUrl=$pending.url}
            else{
                $messageId=[Web.HttpUtility]::ParseQueryString(([Uri]$pending.url).Query)['id']
                if($messageId){$cache.messages[$messageId]=$data}
            }
            $cache.pending.Remove($rid)
        }catch{ # Keep completed requests and retry; responseReceived can precede body availability.
        }
    }
    return $cache
}
function Recovery-Records($Session,[string]$Part) {
    $cache=Update-RecoveryCache $Session
    if($cache.recordsUrl -like ('*'+[Uri]::EscapeDataString($Part)+'*')){return $cache.records}
    return @()
}
$browser=$null;$proxy=$null;$session=$null;$recoverySession=$null
$profileRoot=Join-Path $workerRoot ('profile-'+$job.id)
$ok=$false;$refresh='';$recovery=[string]$job.account.recovery_email
$script:phase='runtime';$failureCode='';$failureLine=0;$failureType='';$browserExit=$null;$nativeError=$null
$script:activeBrowser='Fingerprint Chromium 148'
try {
    $exe=Ensure-Browser
    Heartbeat
    $proxyArgument=''
    $raw=[string]$job.account.proxy
    if($raw){
        if($raw -match '^http://'){$u=[Uri]$raw;$hostName=$u.Host;$port=$u.Port;$auth=$u.UserInfo.Split(':',2);$userName=[Uri]::UnescapeDataString($auth[0]);$password=if($auth.Count -gt 1){[Uri]::UnescapeDataString($auth[1])}else{''}}
        elseif($raw -notmatch '://'){$parts=$raw.Split(':',4);if($parts.Count -lt 2){throw 'Invalid HTTP proxy'};$hostName=$parts[0];$port=[int]$parts[1];$userName=if($parts.Count -ge 4){$parts[2]}else{''};$password=if($parts.Count -ge 4){$parts[3]}else{''}}
        else{throw 'This client worker requires an HTTP proxy'}
        if($port -lt 1 -or $port -gt 65535){throw 'Invalid proxy port'}
        if($userName){$proxy=New-Object TikFlowMailHelper.LocalProxyForwarder($hostName,$port,$userName,$password);$proxyArgument='--proxy-server=http://127.0.0.1:'+$proxy.LocalPort}
        else{$proxyArgument='--proxy-server=http://'+$hostName+':'+$port}
    }
    $script:phase='browser_start'
    Stage 'Opening isolated Chromium on this client'
    New-Item -ItemType Directory -Force -Path $profileRoot|Out-Null
    $browserArguments=@(('--user-data-dir="'+$profileRoot+'"'),'--remote-debugging-port=0','--remote-debugging-address=127.0.0.1','--no-first-run','--no-default-browser-check','--window-size=1000,800')
    if($proxyArgument){$browserArguments+=$proxyArgument}
    if(-not $job.show_browser){$browserArguments+='--headless=new'}
    $browserArguments+='about:blank'
    $browser=Start-ClientBrowser $exe $browserArguments
    $activePort=Join-Path $profileRoot 'DevToolsActivePort'
    $until=[DateTime]::UtcNow.AddSeconds(20)
    while(-not (Test-Path -LiteralPath $activePort)){
        Heartbeat
        if($browser.HasExited){$browserExit=$browser.ExitCode;$failureCode='browser_exited';throw 'Client Chromium exited before CDP was ready'}
        if([DateTime]::UtcNow -gt $until){$failureCode='browser_start_timeout';throw 'Client Chromium did not start'}
        Start-Sleep -Milliseconds 250
    }
    $script:phase='cdp_connect'
    Stage 'Chromium started; connecting to its local control port'
    $port=0
    do {
        try{$port=[int]([IO.File]::ReadAllLines($activePort)[0])}catch{}
        if($port -gt 0 -and $port -le 65535){break}
        if([DateTime]::UtcNow -gt $until){throw 'Chromium control port file was not ready'}
        Heartbeat;Start-Sleep -Milliseconds 150
    }while($true)
    $cdpBase='http://127.0.0.1:'+$port
    do {
        try{$session=New-Tab $cdpBase 'about:blank';break}
        catch{if([DateTime]::UtcNow -gt $until){throw};Heartbeat;Start-Sleep -Milliseconds 300}
    }while($true)
    $script:phase='oauth_setup'
    $null=Cdp $session 'Fetch.enable' @{patterns=@(@{urlPattern='https://localhost*';requestStage='Request'})}
    $rng=New-Object Security.Cryptography.RNGCryptoServiceProvider
    $bytes=New-Object byte[] 48;$rng.GetBytes($bytes);$verifier=[Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    $bytes=New-Object byte[] 32;$rng.GetBytes($bytes);$oauthState=[Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_');$rng.Dispose()
    $sha=[Security.Cryptography.SHA256]::Create();$challenge=[Convert]::ToBase64String($sha.ComputeHash([Text.Encoding]::ASCII.GetBytes($verifier))).TrimEnd('=').Replace('+','-').Replace('/','_');$sha.Dispose()
    $params=@{client_id=$job.client_id;redirect_uri='https://localhost';response_type='code';scope='openid offline_access https://graph.microsoft.com/Mail.Read';code_challenge=$challenge;code_challenge_method='S256';state=$oauthState;login_hint=$job.account.email;prompt='select_account'}
    $query=($params.GetEnumerator()|ForEach-Object {[Uri]::EscapeDataString($_.Key)+'='+[Uri]::EscapeDataString([string]$_.Value)}) -join '&'
    $script:phase='microsoft_login'
    Stage 'Opening Microsoft sign-in on this client'
    $null=Cdp $session 'Page.navigate' @{url=('https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize?'+$query)}
    $deadline=[DateTime]::UtcNow.AddSeconds([int]$job.timeout)
    $submitted=@{};$callback='';$baseline=@{};$lastRecovery=[DateTime]::UtcNow.AddMinutes(-1);$codeRequested=[DateTime]::UtcNow
    while([DateTime]::UtcNow -lt $deadline){
        Heartbeat
        foreach($event in @($session.DrainEventsJson()|ConvertFrom-Json)){
            if($event.method -eq 'Fetch.requestPaused'){
                $callback=[string]$event.params.request.url
                $null=Cdp $session 'Fetch.fulfillRequest' @{requestId=$event.params.requestId;responseCode=200;responseHeaders=@(@{name='Content-Type';value='text/html'});body=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('<h3>OAuth authorization received. Returning result to TikFlow.</h3>'))}
            }
        }
        if($callback){
            $uri=[Uri]$callback;$qs=[Web.HttpUtility]::ParseQueryString($uri.Query)
            if($uri.Scheme -ne 'https' -or $uri.Host -ne 'localhost' -or $qs['state'] -cne $oauthState -or -not $qs['code']){throw 'OAuth callback validation failed'}
            Stage 'Exchanging authorized OAuth code (Mail.Read only)'
            $tokenArgs=@{Uri='https://login.microsoftonline.com/consumers/oauth2/v2.0/token';Method='Post';UseBasicParsing=$true;TimeoutSec=30;Body=@{client_id=$job.client_id;redirect_uri='https://localhost';grant_type='authorization_code';code=$qs['code'];code_verifier=$verifier;scope='openid offline_access https://graph.microsoft.com/Mail.Read'}}
            if($proxyArgument){$tokenArgs['Proxy']=$proxyArgument.Substring('--proxy-server='.Length)}
            $tokens=Invoke-RestMethod @tokenArgs
            if(-not $tokens.refresh_token){throw 'Microsoft did not grant offline access'}
            $graphArgs=@{Uri='https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages?$top=1&$select=id';UseBasicParsing=$true;TimeoutSec=25;Headers=@{Authorization=('Bearer '+$tokens.access_token)}}
            if($tokenArgs.ContainsKey('Proxy')){$graphArgs['Proxy']=$tokenArgs.Proxy}
            $mailCheck=Invoke-RestMethod @graphArgs
            if($null -eq $mailCheck.value){throw 'Mail.Read verification failed'}
            $refresh=[string]$tokens.refresh_token;$ok=$true;break
        }
        try{$p=Probe $session}catch{Start-Sleep -Milliseconds 400;continue}
        $url=[Uri]$p.url
        if($url.Host -and $url.Host -notmatch '(^|\.)(live\.com|microsoftonline\.com|microsoft\.com)$'){throw 'Login left Microsoft domains'}
        $body=([string]$p.body).ToLowerInvariant()
        if($body -match 'account has been locked|account has been suspended|password is incorrect|that password is incorrect'){throw 'Microsoft rejected account access'}
        if($body -match 'captcha|prove.*human|verify.*human|authenticator|approve.*request|phone number'){
            Stage 'Manual security verification required in this client browser';Start-Sleep -Milliseconds 700;continue
        }
        $passwordInput=@($p.inputs|Where-Object {$_.type -eq 'password'}|Select-Object -First 1)[0]
        $next=Button $p '^(next|sign in|continue|ti.p t.c|ti.p theo|dang nh.p|yes|accept|ch.p nh.n)$'
        if($passwordInput -and -not $submitted.password){Stage 'Filling the selected account password';Fill $session $passwordInput ([string]$job.account.password);Click $session $next;$submitted.password=$true;continue}
        $codeBoxes=@(Code-Inputs $p)
        $codeInput=@($codeBoxes|Select-Object -First 1)[0]
        $emailInput=@($p.inputs|Where-Object {$_.type -eq 'email' -or $_.id -eq 'i0116'}|Select-Object -First 1)[0]
        if($emailInput -and $body -notmatch 'security info|verify.*identity|recovery|add.*email|send.*code' -and -not $submitted.email){Stage 'Filling the selected account email';Fill $session $emailInput ([string]$job.account.email);Click $session $next;$submitted.email=$true;continue}
        $sendCode=Button $p '(send.*code|email.*code|g.i m.)'
        if(($sendCode -or $codeInput -or $body -match 'security info|recovery|verify.*identity') -and -not $recoverySession){
            if(-not $recovery){$recovery=([string]$job.account.email).Split('@')[0]+'@fviainboxes.com'}
            if($recovery -notmatch '@fviainboxes\.com$'){Stage 'Use the configured recovery method manually in this browser';Start-Sleep -Milliseconds 700;continue}
            Stage 'Opening the configured recovery mailbox on this client'
            $recoverySession=New-Tab $cdpBase 'https://fviainboxes.com/'
            $null=Cdp $recoverySession 'Network.enable'
            $until=[DateTime]::UtcNow.AddSeconds(20)
            do{$rp=Probe $recoverySession;$recoveryInput=@($rp.inputs|Where-Object {$_.type -in @('text','email')}|Select-Object -First 1)[0];if($recoveryInput){break};Start-Sleep -Milliseconds 400;Heartbeat}while([DateTime]::UtcNow -lt $until)
            if(-not $recoveryInput){throw 'Recovery mailbox UI unavailable'}
            Fill $recoverySession $recoveryInput $recovery.Split('@')[0]
            Click $recoverySession (Button $rp 'Get Email|Refresh')
            Start-Sleep -Seconds 2
            foreach($record in @(Recovery-Records $recoverySession $recovery.Split('@')[0])){
                if([double]$record.createdAt -lt ([DateTimeOffset]$codeRequested).ToUnixTimeSeconds()-5){$baseline[[string]$record.id]=$true}
            }
            $null=Cdp $session 'Page.bringToFront'
            continue
        }
        if($recoverySession -and $sendCode -and -not $submitted.send){
            if($body -match '@' -and $body -notmatch 'fviainboxes\.com'){Stage 'Recovery destination differs; verify manually';Start-Sleep -Milliseconds 700;continue}
            $textInput=@($p.inputs|Where-Object {$_.type -in @('text','email')}|Select-Object -First 1)[0]
            if($textInput -and -not $codeInput){Fill $session $textInput $recovery}
            $codeRequested=[DateTime]::UtcNow;Click $session $sendCode;$submitted.send=$true;Stage 'Waiting for a new Microsoft recovery code';continue
        }
        if($codeInput -and $recoverySession -and -not $submitted.code -and ([DateTime]::UtcNow-$lastRecovery).TotalSeconds -ge 4){
            $rp=Probe $recoverySession;Click $recoverySession (Button $rp 'Get Email|Refresh');Start-Sleep -Milliseconds 600
            $records=@(Recovery-Records $recoverySession $recovery.Split('@')[0])
            foreach($record in @($records|Sort-Object {[double]$_.createdAt} -Descending)){
                if($baseline.ContainsKey([string]$record.id) -or [double]$record.createdAt -lt ([DateTimeOffset]$codeRequested).ToUnixTimeSeconds()-5 -or -not (Is-RecoveryMessage $record)){continue}
                Stage 'Found a new Microsoft security email; reading its code'
                $cache=Update-RecoveryCache $recoverySession
                $messageId=[string]$record.id
                if(-not $cache.messages.ContainsKey($messageId)){
                    $button=Button $rp ([regex]::Escape([string]$record.subject))
                    if(-not $button){$button=Recovery-Item $recoverySession ([string]$record.subject)}
                    Click $recoverySession $button
                    $messageDeadline=[DateTime]::UtcNow.AddSeconds(6)
                    do{
                        Start-Sleep -Milliseconds 250;Heartbeat
                        $cache=Update-RecoveryCache $recoverySession
                    }while(-not $cache.messages.ContainsKey($messageId) -and [DateTime]::UtcNow -lt $messageDeadline)
                }
                $code=if($cache.messages.ContainsKey($messageId)){Extract-RecoveryCode $cache.messages[$messageId]}else{''}
                if($code -and (Submit-RecoveryCode $session $p $code)){
                    $submitted.code=$true;Stage 'Submitted the fresh recovery code'
                    $null=Cdp $session 'Page.bringToFront'
                    break
                }
            }
            $lastRecovery=[DateTime]::UtcNow;continue
        }
        if($body -match 'stay signed in'){Click $session (Button $p '^(no|kh.ng)$');continue}
        if($body -match 'let this app access|permissions requested|cho ph.p .ng d.ng'){
            if($body -match 'send mail as|delete mail'){throw 'Consent exceeds Mail.Read'}
            Click $session (Button $p '^(accept|yes|ch.p nh.n|c.)$');Stage 'Confirming requested read-only OAuth permission';continue
        }
        Stage 'Waiting for Microsoft; complete any additional verification in this client browser'
        Start-Sleep -Milliseconds 500
    }
    if(-not $ok){$failureCode='verification_timeout';throw 'Login or verification timed out'}
} catch {
    $ok=$false
    if(-not $failureCode){$failureCode=$script:phase}
    $failureLine=[int]$_.InvocationInfo.ScriptLineNumber
    $failureType=$_.Exception.GetType().Name
    $cause=$_.Exception
    while($cause){if($cause -is [ComponentModel.Win32Exception]){$nativeError=$cause.NativeErrorCode};$cause=$cause.InnerException}
    # Never upload exception messages: upstream responses may contain credentials.
    try{$null=Event 'log' ('Worker error: '+$failureCode+'; '+$failureType+'; line '+$failureLine)}catch{}
}
finally {
    if($session){$session.Dispose()};if($recoverySession){$recoverySession.Dispose()}
    if($browser){Stop-OwnedProcess $browser};if($proxy){$proxy.Dispose()}
    # Only this job's disposable profile may be removed, never a user browser profile.
    $resolved=[IO.Path]::GetFullPath($profileRoot);$allowed=[IO.Path]::GetFullPath($workerRoot)+[IO.Path]::DirectorySeparatorChar
    if($resolved.StartsWith($allowed,[StringComparison]::OrdinalIgnoreCase) -and (Split-Path -Leaf $resolved) -eq ('profile-'+$job.id)){
        Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
    }
}
$resultPath=Join-Path $workerRoot ('result-'+$job.id+'.bin')
$resultBody=@{job_id=$job.id;lease=$job.lease;kind='result';ok=$ok;refresh_token=$refresh;recovery_email=$recovery;failure_code=$failureCode;failure_line=$failureLine;failure_type=$failureType;browser_exit=$browserExit;native_error=$nativeError}
Write-Protected $resultPath $resultBody
for($attempt=0;$attempt -lt 3;$attempt++){
    try {$null=Api 'event' $resultBody;Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue;break}
    catch {Start-Sleep -Seconds 5}
}
