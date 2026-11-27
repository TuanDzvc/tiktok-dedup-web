$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Web
$source=Join-Path $PSScriptRoot 'static\mail_worker.ps1'
$parseErrors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($source,[ref]$null,[ref]$parseErrors)
if($parseErrors.Count){throw 'Full worker script does not parse in Windows PowerShell'}
if(([IO.File]::ReadAllBytes($source)|Where-Object {$_ -gt 127}).Count){throw 'Worker must remain ASCII compatible with Windows PowerShell 5.1'}
$names=@('Code-Inputs','Is-RecoveryMessage','Extract-RecoveryCode','Submit-RecoveryCode','Update-RecoveryCache','Recovery-Records','Button','Recovery-DestinationMatches','Newest-RecoveryRecord','Event','Stage','Heartbeat','Password-First','Recovery-RequestTime')
foreach($fn in $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in $names},$true)){Invoke-Expression $fn.Extent.Text}
function Assert($Condition,[string]$Message){if(-not $Condition){throw $Message}}
$boxes=@(1..6|ForEach-Object {@{id='digit'+$_;name='';type='tel';max=1;label=''}})
$probe=@{inputs=$boxes;buttons=@()}
Assert (@(Code-Inputs $probe).Count -eq 6) 'Six anonymous digit inputs were not recognized'
Assert (@(Code-Inputs @{inputs=@(@{type='text';max=6;id='otc'})}).Count -eq 1) 'Single OTP input not recognized'
Assert (Is-RecoveryMessage @{from='account-security-noreply@example.com';subject='Your single-use code'}) 'Microsoft single-use subject was rejected'
Assert (-not (Is-RecoveryMessage @{from='other@example.com';subject='Your single-use code'})) 'Unrelated sender accepted'
Assert ((Extract-RecoveryCode @{id='999888';body='Your single-use code is 012345'}) -ceq '012345') 'Code extraction lost a leading zero or used metadata'
Assert ((Extract-RecoveryCode @{id='999888';createdAt=123456;body='No verification code here'}) -ceq '') 'Code extracted from message metadata'
Assert ((Extract-RecoveryCode '<b>Security code: 012345</b>') -ceq '012345') 'HTML code extraction failed'
$global:digits=@()
function Fill($Session,$Element,[string]$Value){$global:digits+=,$Value}
function Fill-CodeInput($Session,$Element,[string]$Value){$global:digits+=,$Value}
function Cdp($Session,[string]$Method,$Parameters){}
function Click($Session,$Element){}
Assert (Submit-RecoveryCode $null $probe '012345') 'Six-box submission failed'
Assert (($global:digits -join '') -ceq '012345' -and $global:digits.Count -eq 6) 'Code was not split into six separate inputs'
$mixed=@(0..5|ForEach-Object {@{id=('codeEntry-'+$_);type='text';max=if($_ -eq 0){6}else{1};label=('Enter code digit '+($_+1))}})
$global:digits=@()
Assert (Submit-RecoveryCode $null @{inputs=$mixed;buttons=@()} '012345') 'Actual Microsoft mixed maxlength layout was not recognized'
Assert ($global:digits.Count -eq 1 -and $global:digits[0] -ceq '012345') 'Microsoft paste-compatible input did not receive the full code'
Assert (Recovery-DestinationMatches 'Send a code to te*****@fviainboxes.com' 'test123@fviainboxes.com') 'Matching masked recovery proof was rejected'
Assert (Recovery-DestinationMatches "We'll send a code to te*****@fviainboxes.com. To verify this is your email, enter it here." 'test123@fviainboxes.com') 'Sentence punctuation broke proof matching'
Assert (-not (Recovery-DestinationMatches 'Send a code to xx*****@fviainboxes.com' 'test123@fviainboxes.com')) 'Different recovery account was accepted'
Assert (-not (Recovery-DestinationMatches 'Send a code to te*****@other.example' 'test123@fviainboxes.com')) 'Different recovery domain was accepted'
$request=[DateTimeOffset]::FromUnixTimeSeconds(1800000000).UtcDateTime
$messages=@(
    @{id='new';createdAt=1800000002;from='account-security-noreply@example.com';subject='Your single-use code'},
    @{id='old';createdAt=1799999940;from='account-security-noreply@example.com';subject='Your single-use code'},
    @{id='newest';createdAt=1800000005;from='account-security-noreply@example.com';subject='Your single-use code'},
    @{id='unrelated';createdAt=1800000008;from='other@example.com';subject='Your single-use code'}
)
$selected=@(Newest-RecoveryRecord $messages @{} $request)
Assert ($selected.Count -eq 1 -and $selected[0].id -eq 'newest') 'Did not select exactly the newest security mail'
Assert (@(Newest-RecoveryRecord $messages @{new=$true;newest=$true} $request).Count -eq 0) 'Selected old or previously seen code'
Assert (@(Newest-RecoveryRecord @($messages[1]) @{} $request).Count -eq 0) 'Accepted a mail received before the request'
Assert (Is-RecoveryMessage @{from='account-security-noreply@accountprotection.microsoft.com';subject='Personal Microsoft account security code'}) 'Post-password security-code subject rejected'
$security=@{id='security';createdAt=1800000004;from='account-security-noreply@accountprotection.microsoft.com';subject='Personal Microsoft account security code'}
$securityPick=@(Newest-RecoveryRecord (@($messages)+@($security)) @{} $request $true)
Assert ($securityPick.Count -eq 1 -and $securityPick[0].id -eq 'security') 'Selected newer passwordless code instead of post-password security code'
Assert ((Extract-RecoveryCode '<style>.x{color:#123456}</style><p>Security code: 012345</p>') -ceq '012345') 'Security code was confused with HTML styling'
Assert (@(Code-Inputs @{body='Enter your code';inputs=@(@{id='verificationInput';type='text';max=-1;name='';label=''})}).Count -eq 1) 'Unlabelled security input was not recognized'
Assert (@(Code-Inputs @{body='Verify your email';inputs=@(@{id='proof-confirmation-email-input';type='text';max=-1;name='';label=''})}).Count -eq 0) 'Recovery email field was mistaken for OTP'
$skewed=@{id='arrived-one-second-before-client-clock';createdAt=1799999999;from='account-security-noreply@accountprotection.microsoft.com';subject='Personal Microsoft account security code'}
Assert (@(Newest-RecoveryRecord @($skewed) @{} $request $true).Count -eq 1) 'One-second clock skew rejected a fresh security mail'
Assert (@(Newest-RecoveryRecord @($skewed) @{'arrived-one-second-before-client-clock'=$true} $request $true).Count -eq 0) 'Clock tolerance reused a baseline message'
$script:recoveryClocks=@{}
$clockSession=New-Object PSObject
$script:recoveryClocks[[string]$clockSession.GetHashCode()]=@{server=$request;sampled=[DateTime]::UtcNow}
$clockTime=Recovery-RequestTime $clockSession
Assert ([Math]::Abs(($clockTime-$request).TotalSeconds) -lt 2) 'Recovery request time ignored mailbox server clock'
$script:recoveryCache=@{}
$global:fixtureEvents=@(
    @{method='Page.frameNavigated';params=@{}},
    @{method='Network.responseReceived';params=@{requestId='r1';response=@{url='https://fixture.invalid/messages?email=fixture'}}}
)
$session=New-Object PSObject
$session|Add-Member ScriptMethod DrainEventsJson { $value=ConvertTo-Json -InputObject @($global:fixtureEvents) -Depth 10 -Compress;$global:fixtureEvents=@();return $value }
$global:bodyCalls=0
function Cdp($Session,[string]$Method,$Parameters){
    $global:bodyCalls++
    if($global:bodyCalls -eq 1){throw 'Fixture body is not yet ready'}
    return @{body='{"result":[{"id":"fixture-message","subject":"Your single-use code","from":"account-security-noreply@example.com"}]}';base64Encoded=$false}
}
$null=Update-RecoveryCache $session
Assert ($global:bodyCalls -eq 0) 'Read response body before Network.loadingFinished'
$global:fixtureEvents=@(
    @{method='Network.dataReceived';params=@{requestId='r1'}},
    @{method='Network.loadingFinished';params=@{requestId='r1'}}
)
$null=Update-RecoveryCache $session
Assert ($global:bodyCalls -eq 1) 'Response body was never attempted'
$records=@(Recovery-Records $session 'fixture')
Assert ($records.Count -eq 1) 'Delayed response body was discarded instead of retried'
Assert (@(Recovery-Records $session 'fixture').Count -eq 1) 'Inbox records disappeared on the next poll'
Assert (@(Recovery-Records $session 'different-mailbox').Count -eq 0) 'Records leaked across mailbox names'
$api=$ast.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Recovery-Api'},$true)
Invoke-Expression $api.Extent.Text
function Evaluate($Session,[string]$Expression){$script:recoveryExpression=$Expression;return [PSCustomObject]@{result=@(@{id='fixture-message'})}}
$null=Recovery-Api $session '/message' @{id='fixture-id';username='fixture';domain='fviainboxes.com'}
Assert ($script:recoveryExpression.Contains('id=fixture-id') -and $script:recoveryExpression.Contains('username=fixture') -and $script:recoveryExpression.Contains('domain=fviainboxes.com')) 'Message fetch omitted required mailbox parameters'
Assert ($script:recoveryExpression.Contains('cache:"no-store"') -and $script:recoveryExpression.Contains('location.origin')) 'Client inbox fetch lacks cache/origin safeguards'
Assert (@(Recovery-Records $session 'fixture').Count -eq 1) 'Direct client fetch did not return inbox records'
$script:recoveryClocks=@{}
function Evaluate($Session,[string]$Expression){return [PSCustomObject]@{__tfMailPayload=[PSCustomObject]@{result=@(@{id='clock-fixture'})};serverEpoch=1800000000}}
$clockPayload=Recovery-Api $session '/messages' @{username='fixture';domain='fviainboxes.com'}
Assert ($clockPayload.result[0].id -eq 'clock-fixture') 'Mailbox server-clock wrapper changed the inbox payload'
Assert ($script:recoveryClocks.ContainsKey([string]$session.GetHashCode())) 'Mailbox HTTP Date was not captured'
function Evaluate($Session,[string]$Expression){return [PSCustomObject]@{__tfMailPayload='Security code: 012345';serverEpoch=1800000000}}
Assert ((Recovery-Api $session '/message' @{id='fixture';username='fixture';domain='fviainboxes.com'}) -ceq 'Security code: 012345') 'Server-clock wrapper corrupted string message body'
$job=@{id='fixture';lease='fixture'}
$global:apiFixture='network'
function Api([string]$Action,$Body){
    if($global:apiFixture -eq 'network'){throw (New-Object Net.WebException('Fixture temporary outage'))}
    if($global:apiFixture -eq 'cancel'){return @{cancel=$true}}
    return @{cancel=$false}
}
$script:lastLog=''
$script:lastHeartbeat=[DateTime]::UtcNow.AddMinutes(-1)
Stage 'Fixture inspection'
Heartbeat
Assert ($script:lastLog -eq '') 'Failed log upload was marked delivered'
$global:apiFixture='ok'
Stage 'Fixture inspection'
Assert ($script:lastLog -eq 'Fixture inspection') 'Log was not retried after network recovery'
$global:apiFixture='cancel'
$cancelled=$false
try{Stage 'Fixture stop'}catch [OperationCanceledException]{$cancelled=$true}
Assert $cancelled 'Explicit web cancellation was swallowed'
# Password-first routing across actual Microsoft page variants.
function Stage([string]$Text){}
$global:routeClicks=@();$global:routeFills=@()
function Click($Session,$Element){if($Element){$global:routeClicks+=,[string]$Element.text}}
function Fill($Session,$Element,[string]$Value){$global:routeFills+=,[string]$Element.id}
$state=@{}
$confirm=@{inputs=@(@{id='proof-confirmation-email-input';type='text'});buttons=@(@{text='Send code'},@{text='Other ways to sign in'})}
Assert (Password-First $null $confirm $state 'fixture-password') 'Recovery must be blocked before password submission'
Assert ($global:routeClicks[-1] -eq 'Other ways to sign in' -and $global:routeFills.Count -eq 0) 'Recovery email was filled or code requested before password'
# Repeated initial proof screens must never fill recovery, even when Send code
# is available. Only a password field may receive text before password submit.
Assert (Password-First $null $confirm $state 'fixture-password') 'Initial confirmation page did not remain in password routing'
Assert ($global:routeFills.Count -eq 0 -and $global:routeClicks[-1] -eq 'Other ways to sign in') 'Initial recovery form was touched'
$initialCode=@{inputs=$mixed;buttons=@(@{text="Didn't get a code?"})}
Assert (Password-First $null $initialCode $state 'fixture-password') 'Code page must remain in password routing'
Assert ($global:routeClicks[-1] -eq "Didn't get a code?" -and $global:routeFills.Count -eq 0) 'Unexpected email or code entry before password'
$choice=@{inputs=@();buttons=@(@{text='Use your password'},@{text='Send a code to fixture@example.com'})}
Assert (Password-First $null $choice $state 'fixture-password') 'Password method selection must reprobe'
Assert ($global:routeClicks[-1] -eq 'Use your password') 'Code method was selected instead of password'
$form=@{inputs=@(@{id='password';type='password'});buttons=@(@{text='Sign in'})}
Assert (Password-First $null $form $state 'fixture-password') 'Password submission must reprobe'
Assert ($state.password -and $global:routeFills[-1] -eq 'password') 'Password was not submitted'
Assert (-not (Password-First $null $confirm $state 'fixture-password')) 'Recovery remained blocked after password submission'
Write-Output 'PASS: six-box OTP, single-use subject, sender matching, safe extraction, delayed body retry and retained inbox'
