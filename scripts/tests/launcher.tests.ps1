<#
    Tests for the pure parts of vault.ps1 (token shape, address, process record, token file) and
    for how it is called. They need no installed packages and run in Windows PowerShell 5.1 and
    PowerShell 7:  powershell -NoProfile -File scripts\tests\launcher.tests.ps1
    The end-to-end check (install, run, stop against a real server) is a CI step.
#>
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$launcher = Join-Path $repo 'vault.ps1'
. $launcher

$script:Failures = 0

function Assert-That {
    param([string]$Name, [bool]$Condition)
    if ($Condition) {
        Write-Host "ok   $Name"
    }
    else {
        Write-Host "FAIL $Name"
        $script:Failures++
    }
}

function Get-ThrownMessage {
    param([scriptblock]$Block)
    try {
        & $Block | Out-Null
    }
    catch {
        return $_.Exception.Message
    }
    return $null
}

$token = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ-_0123'   # 49 characters of the right kind

# --- the shape of a token ---------------------------------------------------------------------
Assert-That 'a well-formed token passes' (Test-TokenShape $token)
Assert-That 'the shortest allowed token (32) passes' (Test-TokenShape ('a' * 32))
Assert-That 'the longest allowed token (128) passes' (Test-TokenShape ('a' * 128))
Assert-That 'a token of 31 characters fails' (-not (Test-TokenShape ('a' * 31)))
Assert-That 'a token of 129 characters fails' (-not (Test-TokenShape ('a' * 129)))
Assert-That 'a trailing newline fails' (-not (Test-TokenShape ($token + "`n")))
Assert-That 'a trailing carriage return fails' (-not (Test-TokenShape ($token + "`r")))
Assert-That 'a trailing space fails' (-not (Test-TokenShape ($token + ' ')))
Assert-That 'a leading space fails' (-not (Test-TokenShape (' ' + $token)))
Assert-That 'a mark outside the alphabet fails' (-not (Test-TokenShape (('a' * 40) + '!')))
Assert-That 'null fails' (-not (Test-TokenShape $null))
Assert-That 'a number fails' (-not (Test-TokenShape 12345))

# --- the address the browser opens ------------------------------------------------------------
Assert-That 'the token goes in the fragment' `
    ((Get-AppAddress -HostName '127.0.0.1' -Port 8765 -Token $token) -ceq "http://127.0.0.1:8765/#token=$token")
Assert-That 'an IPv6 host is bracketed' `
    ((Get-AppAddress -HostName '::1' -Port 9000 -Token $token) -ceq "http://[::1]:9000/#token=$token")
$secret = 'PATIENT-DIAGNOSIS-CONFIDENTIAL!'
$message = Get-ThrownMessage { Get-AppAddress -HostName '127.0.0.1' -Port 8765 -Token $secret }
Assert-That 'a wrong token is refused' ($null -ne $message)
Assert-That 'a wrong token is never echoed' (($null -ne $message) -and (-not $message.Contains($secret)))

# --- the record of the process that run started ------------------------------------------------
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("vault-launcher-tests-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
try {
    $script:RunDir = Join-Path $work 'run'
    $script:RecordFile = Join-Path $script:RunDir 'server.json'
    $self = Get-Process -Id $PID

    Assert-That 'no record means no server' ($null -eq (Get-RunningServer))

    $facts = [pscustomobject]@{ host = '127.0.0.1'; port = 8765; vault = 'C:\AIVault' }
    Write-Record $self $facts
    $found = Get-RunningServer
    Assert-That 'a record of a live process with the same start time is found' `
        (($null -ne $found) -and ($found.Process.Id -eq $PID))
    Assert-That 'the settings the server was started with come back with it' `
        (($found.Host -ceq '127.0.0.1') -and ($found.Port -eq 8765) -and ($found.Vault -ceq 'C:\AIVault'))

    # A record without settings (or with unusable ones) still finds the process, so stop works,
    # but run is told that the settings are not known.
    $now = Get-StartStamp $self
    foreach ($case in @(
            @{ name = 'no settings'; extra = '' },
            @{ name = 'port 0'; extra = ', "host": "127.0.0.1", "port": 0, "vault": "C:\\v"' },
            @{ name = 'port 70000'; extra = ', "host": "127.0.0.1", "port": 70000, "vault": "C:\\v"' },
            @{ name = 'port true'; extra = ', "host": "127.0.0.1", "port": true, "vault": "C:\\v"' },
            @{ name = 'port text'; extra = ', "host": "127.0.0.1", "port": "x", "vault": "C:\\v"' },
            @{ name = 'empty host'; extra = ', "host": "", "port": 8765, "vault": "C:\\v"' },
            @{ name = 'empty vault'; extra = ', "host": "127.0.0.1", "port": 8765, "vault": ""' }
        )) {
        Set-Content -LiteralPath $script:RecordFile -Encoding ASCII -Value ('{"pid": ' + $PID + ', "started": ' + $now + $case.extra + '}')
        $partial = Get-RunningServer
        $lacksASetting = $false
        if ($null -ne $partial) {
            $lacksASetting = ($null -eq $partial.Host) -or ($null -eq $partial.Port) -or ($null -eq $partial.Vault)
        }
        Assert-That ("a record with $($case.name) finds the process and lacks a setting") `
            (($null -ne $partial) -and ($partial.Process.Id -eq $PID) -and $lacksASetting)
    }
    Write-Record $self $facts

    $record = Get-Content -LiteralPath $script:RecordFile -Raw | ConvertFrom-Json
    $record.started = 630822816000000000   # the year 2000, as ticks
    $record | ConvertTo-Json | Set-Content -LiteralPath $script:RecordFile -Encoding ASCII
    Assert-That 'a process that only reuses the number is not taken for the server' `
        ($null -eq (Get-RunningServer))
    Assert-That 'that stale record is removed' (-not (Test-Path -LiteralPath $script:RecordFile))

    $record.pid = 2147480000
    $record | ConvertTo-Json | Set-Content -LiteralPath $script:RecordFile -Encoding ASCII
    Assert-That 'a record of a process that is gone is not found' ($null -eq (Get-RunningServer))
    Assert-That 'that record is removed' (-not (Test-Path -LiteralPath $script:RecordFile))

    foreach ($garbage in @('not json', '{}', '{"pid": "x", "started": 1}', '[]')) {
        Set-Content -LiteralPath $script:RecordFile -Value $garbage -Encoding ASCII
        Assert-That "an unreadable record ($garbage) is not found" ($null -eq (Get-RunningServer))
        Assert-That "an unreadable record ($garbage) is removed" (-not (Test-Path -LiteralPath $script:RecordFile))
    }

    # --- the token file of the vault ---------------------------------------------------------
    $vault = Join-Path $work 'vault'
    New-Item -ItemType Directory -Path $vault | Out-Null
    Assert-That 'a missing token file is refused' ($null -ne (Get-ThrownMessage { Read-Token $vault }))
    Set-Content -LiteralPath (Join-Path $vault 'local-token') -Value $token -Encoding ASCII
    Assert-That 'a token file is read (the line break is not part of the token)' ((Read-Token $vault) -ceq $token)
    Set-Content -LiteralPath (Join-Path $vault 'local-token') -Value $secret -Encoding ASCII
    $message = Get-ThrownMessage { Read-Token $vault }
    Assert-That 'a token file that holds something else is refused' ($null -ne $message)
    Assert-That 'and what it held is never echoed' (($null -ne $message) -and (-not $message.Contains($secret)))
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}

# --- how the script is called ------------------------------------------------------------------
$shell = (Get-Process -Id $PID).Path
$null = & $shell -NoProfile -File $launcher
Assert-That 'no command shows the usage and exits with 2' ($LASTEXITCODE -eq 2)
# Windows PowerShell 5.1 turns a native program's error output into an exception while the
# preference is Stop, so look at the exit code with the preference relaxed.
$ErrorActionPreference = 'Continue'
$null = & $shell -NoProfile -File $launcher 'bogus' 2>&1
$bogusExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
Assert-That 'an unknown command is refused' ($bogusExit -ne 0)

# --- the token is never printed -----------------------------------------------------------------
$printing = Select-String -LiteralPath $launcher -Pattern '^\s*(Write-Host|Write-Output|Write-Warning|Write-Verbose|Write-Information|\[Console\])' |
    Where-Object { $_.Line -match '\$token|\$\{token\}|\$address|\$Token' }
Assert-That 'no line prints the token or the address that holds it' ($null -eq $printing)

if ($script:Failures -gt 0) {
    Write-Host "$($script:Failures) check(s) failed"
    exit 1
}
Write-Host 'all launcher checks passed'
exit 0
