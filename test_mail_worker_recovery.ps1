$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Web
$source=Join-Path $PSScriptRoot 'static\mail_worker.ps1'
$ast=[System.Management.Automation.Language.Parser]::ParseFile($source,[ref]$null,[ref]$null)
$names=@('Code-Inputs','Is-RecoveryMessage','Extract-RecoveryCode','Submit-RecoveryCode','Update-RecoveryCache','Recovery-Records','Button')
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
function Click($Session,$Element){}
Assert (Submit-RecoveryCode $null $probe '012345') 'Six-box submission failed'
Assert (($global:digits -join '') -ceq '012345' -and $global:digits.Count -eq 6) 'Code was not split into six separate inputs'
$script:recoveryCache=@{}
$global:fixtureEvents=@(@{method='Network.responseReceived';params=@{requestId='r1';response=@{url='https://fixture.invalid/messages?email=fixture'}}})
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
$global:fixtureEvents=@(@{method='Network.loadingFinished';params=@{requestId='r1'}})
$null=Update-RecoveryCache $session
Assert ($global:bodyCalls -eq 1) 'Response body was never attempted'
$records=@(Recovery-Records $session 'fixture')
Assert ($records.Count -eq 1) 'Delayed response body was discarded instead of retried'
Assert (@(Recovery-Records $session 'fixture').Count -eq 1) 'Inbox records disappeared on the next poll'
Assert (@(Recovery-Records $session 'different-mailbox').Count -eq 0) 'Records leaked across mailbox names'
Write-Output 'PASS: six-box OTP, single-use subject, sender matching, safe extraction, delayed body retry and retained inbox'
