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

    # --- a vault folder whose name is not ASCII ----------------------------------------------
    # Built from code points: Windows PowerShell 5.1 reads this file in the ANSI code page, so a
    # letter typed here would not arrive as written.
    $accented = 'C:\Users\Jos' + [char]0x00E9 + '\AIVault-' + [char]0x00DF + [char]0x4E2D + [char]::ConvertFromUtf32(0x1F4C1)
    Write-Record $self ([pscustomobject]@{ host = '127.0.0.1'; port = 8765; vault = $accented })
    $bytes = [System.IO.File]::ReadAllBytes($script:RecordFile)
    Assert-That 'the record has no byte order mark' (-not (($bytes.Length -ge 3) -and ($bytes[0] -eq 0xEF) -and ($bytes[1] -eq 0xBB) -and ($bytes[2] -eq 0xBF)))
    Assert-That 'the record keeps a vault folder with non-ASCII letters' `
        ((Get-RunningServer).Vault -ceq $accented)
    Assert-That 'the record does not hold a question mark for a letter' `
        (-not ([System.IO.File]::ReadAllText($script:RecordFile).Contains('?')))
    Remove-Record

    # --- a port that something else holds ------------------------------------------------------
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $taken = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    try {
        Assert-That 'a port that accepts connections is in use' (Test-PortInUse '127.0.0.1' $taken)
        Assert-That 'a wildcard address is probed on loopback' (Test-PortInUse '0.0.0.0' $taken)
        $script:BackendDir = Join-Path $work 'no-backend'
        $message = Get-ThrownMessage { Start-Backend ([pscustomobject]@{ host = '127.0.0.1'; port = $taken; vault = 'C:\v' }) }
        Assert-That 'starting on a taken port is refused before anything is started' `
            (($null -ne $message) -and ($message -match 'already answers') -and ($message -notmatch 'not installed'))
        Assert-That 'and no record or log is left behind' (@(Get-ChildItem -LiteralPath $script:RunDir -Force).Count -eq 0)
    }
    finally {
        $listener.Stop()
    }
    Assert-That 'a port that nothing holds is free' (-not (Test-PortInUse '127.0.0.1' $taken))

    # --- stopping and start-up failures ----------------------------------------------------------
    $shellPath = (Get-Process -Id $PID).Path
    function New-Sleeper {
        Microsoft.PowerShell.Management\Start-Process -FilePath $shellPath -PassThru -WindowStyle Hidden `
            -ArgumentList '-NoProfile', '-Command', 'Start-Sleep -Seconds 120'
    }
    $facts = [pscustomobject]@{ host = '127.0.0.1'; port = 8765; vault = 'C:\v' }

    $sleeper = New-Sleeper
    try {
        Write-Record $sleeper $facts
        Stop-Backend $sleeper
        Assert-That 'a server that can be ended is ended and its record removed' `
            ($sleeper.HasExited -and -not (Test-Path -LiteralPath $script:RecordFile))
    }
    finally {
        if (-not $sleeper.HasExited) { $sleeper.Kill() }
    }

    # A process that refuses to end (stood in for by an object that says it is still running).
    $stubborn = [pscustomobject]@{ Id = 2147480000; HasExited = $false }
    $stubborn | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) $false }
    Write-Record $self $facts
    $message = Get-ThrownMessage { Stop-Backend $stubborn }
    Assert-That 'a refused stop is reported' (($null -ne $message) -and ($message -match 'could not be stopped'))
    Assert-That 'and the record is kept, so stop can be tried again' (Test-Path -LiteralPath $script:RecordFile)

    # A process that is ended but does not exit within the wait.
    $sleeper = New-Sleeper
    try {
        $slow = [pscustomobject]@{ Id = $sleeper.Id; HasExited = $false }
        $slow | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) $false }
        $message = Get-ThrownMessage { Stop-Backend $slow }
        Assert-That 'a server that does not exit in time is reported' (($null -ne $message) -and ($message -match 'did not exit'))
        Assert-That 'and its record is kept' (Test-Path -LiteralPath $script:RecordFile)
    }
    finally {
        if (-not $sleeper.HasExited) { $sleeper.Kill() }
    }
    Remove-Record

    # A record that names another process belongs to a server started since (dev's server was
    # ended by "stop", and "run" started another): ending the old one must not remove it.
    $other = New-Sleeper
    $old = New-Sleeper
    try {
        Write-Record $other $facts
        Stop-Backend $old
        Assert-That 'ending a server keeps a record that names another process' `
            ($old.HasExited -and ((Get-RunningServer).Process.Id -eq $other.Id))
        $otherStarted = Get-StartStamp $other
        Stop-Backend $old 630822816000000000
        Assert-That 'and so does ending one that has already exited' ((Get-RunningServer).Process.Id -eq $other.Id)
        Stop-Backend $other $otherStarted
        Assert-That 'the record of the process that is ended is removed' (-not (Test-Path -LiteralPath $script:RecordFile))
    }
    finally {
        foreach ($p in $other, $old) { if (-not $p.HasExited) { $p.Kill() } }
    }
    Remove-Record

    # --- the launch id: the browser goes only to the process that was started -------------------
    $launch = New-LaunchId
    Assert-That 'a launch id has the shape the server and Vite echo' (Test-TokenShape $launch)
    Assert-That 'each launch id is new' ($launch -cne (New-LaunchId))

    $before = [Environment]::GetEnvironmentVariable('VAULT_LAUNCH_ID', 'Process')
    $seen = Start-WithLaunchId $launch { [Environment]::GetEnvironmentVariable('VAULT_LAUNCH_ID', 'Process') }
    Assert-That 'a process started inside the block gets the launch id' ($seen -ceq $launch)
    Assert-That 'and it is taken out of the environment afterwards' `
        ([Environment]::GetEnvironmentVariable('VAULT_LAUNCH_ID', 'Process') -ceq $before)
    $null = Get-ThrownMessage { Start-WithLaunchId $launch { throw 'boom' } }
    Assert-That 'also when the block fails' `
        ([Environment]::GetEnvironmentVariable('VAULT_LAUNCH_ID', 'Process') -ceq $before)

    # A small HTTP server on a free port that gives every request the same answer.
    function Start-FakeServer {
        param([string]$Body)
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
        $listener.Start()
        $shell = [powershell]::Create().AddScript({
                param($listener, $body)
                while ($true) {
                    try { $client = $listener.AcceptTcpClient() } catch { return }
                    try {
                        $stream = $client.GetStream()
                        $reader = New-Object System.IO.StreamReader($stream)
                        while ($true) {
                            $line = $reader.ReadLine()
                            if ($null -eq $line -or $line -eq '') { break }
                        }
                        $content = [System.Text.Encoding]::UTF8.GetBytes($body)
                        $head = [System.Text.Encoding]::ASCII.GetBytes("HTTP/1.1 200 OK`r`nContent-Type: application/json`r`nContent-Length: $($content.Length)`r`nConnection: close`r`n`r`n")
                        $stream.Write($head, 0, $head.Length)
                        $stream.Write($content, 0, $content.Length)
                        $stream.Flush()
                    }
                    catch { }
                    finally { $client.Close() }
                }
            }).AddArgument($listener).AddArgument($Body)
        return [pscustomobject]@{
            Listener = $listener
            Shell    = $shell
            Pending  = $shell.BeginInvoke()
            Url      = "http://127.0.0.1:$(([System.Net.IPEndPoint]$listener.LocalEndpoint).Port)/api/health"
        }
    }
    function Stop-FakeServer {
        param($Fake)
        $Fake.Listener.Stop()
        try { $null = $Fake.Shell.EndInvoke($Fake.Pending) } catch { }
        $Fake.Shell.Dispose()
    }

    $alive = New-Sleeper
    try {
        $cases = @(
            @{ name = 'the same launch id'; body = '{"status": "ok", "launch": "' + $launch + '"}'; expect = $null },
            @{ name = 'another launch id'; body = '{"status": "ok", "launch": "' + (New-LaunchId) + '"}'; expect = 'another program answers' },
            @{ name = 'the id in another case'; body = '{"status": "ok", "launch": "' + $launch.ToUpperInvariant() + '"}'; expect = 'another program answers' },
            @{ name = 'no launch id'; body = '{"status": "ok", "launch": null}'; expect = 'another program answers' },
            @{ name = 'no launch field'; body = '{"status": "ok"}'; expect = 'another program answers' },
            @{ name = 'text that is not JSON'; body = 'hello'; expect = 'another program answers' }
        )
        foreach ($case in $cases) {
            $fake = Start-FakeServer $case.body
            try {
                $message = Get-ThrownMessage { Wait-Launch -Url $fake.Url -Launch $launch -Process $alive -Name 'server' -Hint 'see the log' -Minutes 0.5 }
            }
            finally {
                Stop-FakeServer $fake
            }
            if ($null -eq $case.expect) {
                Assert-That "an answer with $($case.name) is ready" ($null -eq $message)
            }
            else {
                Assert-That "an answer with $($case.name) is refused" (($null -ne $message) -and ($message -match $case.expect))
                Assert-That "and the refusal does not echo the launch id ($($case.name))" (($null -ne $message) -and (-not $message.Contains($launch)))
            }
        }
        $closed = "http://127.0.0.1:$taken/api/health"   # the listener on it was stopped above
        $message = Get-ThrownMessage { Wait-Launch -Url $closed -Launch $launch -Process $alive -Name 'server' -Hint 'see the log' -Minutes 0.02 }
        Assert-That 'a server that never answers is given up on' (($null -ne $message) -and ($message -match 'did not answer'))
    }
    finally {
        if (-not $alive.HasExited) { $alive.Kill() }
    }
    $null = $alive.WaitForExit(10000)
    $message = Get-ThrownMessage { Wait-Launch -Url $closed -Launch $launch -Process $alive -Name 'server' -Hint 'see the log' -Minutes 1 }
    Assert-That 'a server that has exited is reported at once' (($null -ne $message) -and ($message -match 'stopped while starting'))

    # Start-Backend gives the server a launch id of its own and waits for that one.
    $script:childLaunch = $null
    $script:waitedFor = $null
    $script:started = $null
    & {
        function Get-BackendPython { return 'unused' }
        function Start-Process {
            $script:childLaunch = [Environment]::GetEnvironmentVariable('VAULT_LAUNCH_ID', 'Process')
            $script:started = New-Sleeper
            return $script:started
        }
        function Wait-Launch { param($Url, $Launch, $Process, $Name, $Hint, $Minutes, $SlowNote) $script:waitedFor = $Launch; throw 'another program answers' }
        try {
            $null = Get-ThrownMessage { Start-Backend ([pscustomobject]@{ host = '127.0.0.1'; port = $taken; vault = 'C:\v' }) }
            Assert-That 'the server is started with a launch id' (Test-TokenShape $script:childLaunch)
            Assert-That 'and the launcher waits for that id' ($script:waitedFor -ceq $script:childLaunch)
            Assert-That 'a server that is not the one answering is ended' ($null -ne $script:started -and $script:started.HasExited)
            Assert-That 'and its record removed' (-not (Test-Path -LiteralPath $script:RecordFile))
        }
        finally {
            if ($null -ne $script:started -and -not $script:started.HasExited) { $script:started.Kill() }
        }
    }

    # A record that cannot be written after the server has started: the server must not be left
    # running. The functions are replaced inside this block only.
    $script:started = $null
    & {
        function Get-BackendPython { return 'unused' }
        function Start-Process {
            $script:started = New-Sleeper
            return $script:started
        }
        function Write-Record { throw 'the record file is locked' }
        try {
            $message = Get-ThrownMessage { Start-Backend ([pscustomobject]@{ host = '127.0.0.1'; port = $taken; vault = 'C:\v' }) }
            Assert-That 'start-up fails with the reason when the record cannot be written' `
                (($null -ne $message) -and ($message -match 'locked'))
            Assert-That 'and the server that was started is ended' ($null -ne $script:started -and $script:started.HasExited)
        }
        finally {
            if ($null -ne $script:started -and -not $script:started.HasExited) { $script:started.Kill() }
        }
    }

    # --- the launch id in the record -------------------------------------------------------------
    $recordedLaunch = New-LaunchId
    Write-Record $self $facts $recordedLaunch
    Assert-That 'the record keeps the launch id the server was started with' `
        ((Get-RunningServer).Launch -ceq $recordedLaunch)
    Write-Record $self $facts
    Assert-That 'a record without a launch id still finds the process, without one' `
        (((Get-RunningServer).Process.Id -eq $PID) -and ($null -eq (Get-RunningServer).Launch))
    $now = Get-StartStamp $self
    foreach ($case in @(
            @{ name = 'a short id'; value = '"abc"' },
            @{ name = 'a number'; value = '12345' },
            @{ name = 'an id with a trailing newline'; value = '"' + $recordedLaunch + '\n"' },
            @{ name = 'null'; value = 'null' }
        )) {
        Set-Content -LiteralPath $script:RecordFile -Encoding ASCII -Value ('{"pid": ' + $PID + ', "started": ' + $now + ', "host": "127.0.0.1", "port": 8765, "vault": "C:\\v", "launch": ' + $case.value + '}')
        $read = Get-RunningServer
        Assert-That "a record with $($case.name) as launch id finds the process but no id" `
            (($null -ne $read) -and ($null -eq $read.Launch))
    }
    Remove-Record

    # --- stop: a clean shutdown first, the forced end only as the fallback ------------------------
    # A stand-in for the server on a free port. GET /api/health answers $Health; POST /api/shutdown
    # answers $Status and, with -Obey, ends $Process as a server that shuts down would. The first
    # line and the headers of every request are kept, to see what was sent where.
    function Start-StopServer {
        param([string]$Health, [int]$Status = 202, [object]$Process, [switch]$Obey)
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
        $listener.Start()
        $seen = [System.Collections.ArrayList]::Synchronized((New-Object System.Collections.ArrayList))
        $shell = [powershell]::Create().AddScript({
                param($listener, $health, $status, $process, $obey, $seen)
                while ($true) {
                    try { $client = $listener.AcceptTcpClient() } catch { return }
                    try {
                        $stream = $client.GetStream()
                        $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::ASCII)
                        $lines = New-Object System.Collections.ArrayList
                        while ($true) {
                            $line = $reader.ReadLine()
                            if ($null -eq $line -or $line -eq '') { break }
                            $null = $lines.Add($line)
                        }
                        $null = $seen.Add(($lines -join "`n"))
                        $first = if ($lines.Count -gt 0) { [string]$lines[0] } else { '' }
                        $code = 404
                        $body = '{"detail": "Not Found"}'
                        if ($first.StartsWith('GET /api/health ')) { $code = 200; $body = $health }
                        elseif ($first.StartsWith('POST /api/shutdown ')) { $code = $status; $body = '{"stopping": true}' }
                        $content = [System.Text.Encoding]::UTF8.GetBytes($body)
                        $head = [System.Text.Encoding]::ASCII.GetBytes("HTTP/1.1 $code X`r`nContent-Type: application/json`r`nContent-Length: $($content.Length)`r`nConnection: close`r`n`r`n")
                        $stream.Write($head, 0, $head.Length)
                        $stream.Write($content, 0, $content.Length)
                        $stream.Flush()
                        if ($obey -and $code -eq 202 -and -not $process.HasExited) { $process.Kill() }
                    }
                    catch { }
                    finally { $client.Close() }
                }
            }).AddArgument($listener).AddArgument($Health).AddArgument($Status).AddArgument($Process).AddArgument([bool]$Obey).AddArgument($seen)
        return [pscustomobject]@{
            Listener = $listener
            Shell    = $shell
            Pending  = $shell.BeginInvoke()
            Port     = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
            Seen     = $seen
        }
    }

    # A vault folder whose name is not ASCII, with a token in it (built from code points, as above).
    $stopVault = Join-Path $work ('vault-Jos' + [char]0x00E9 + '-' + [char]0x4E2D)
    New-Item -ItemType Directory -Path $stopVault | Out-Null
    $stopToken = New-LaunchId   # any value of the token's shape
    [System.IO.File]::WriteAllText((Join-Path $stopVault 'local-token'), $stopToken + "`n", (New-Object System.Text.UTF8Encoding($false)))

    function Invoke-StopCase {
        <# Record $Process as the server on $Port with $Launch, run Stop-Server with a short wait and
           return whether it was clean, what it printed and the message it threw. #>
        param([object]$Process, [int]$Port, [string]$Launch, [string]$Vault = $stopVault, [double]$Seconds = 5)
        Write-Record $Process ([pscustomobject]@{ host = '127.0.0.1'; port = $Port; vault = $Vault }) $Launch
        $running = Get-RunningServer
        $clean = $null
        $thrown = $null
        $printed = @()
        try {
            $printed = @(& { Stop-Server $running -Seconds $Seconds } 6>&1)
        }
        catch {
            $thrown = $_.Exception.Message
        }
        $results = @($printed | Where-Object { $_ -is [bool] })
        if ($results.Count -eq 1) { $clean = $results[0] }
        $text = (@($printed | Where-Object { $_ -isnot [bool] } | ForEach-Object { [string]$_ }) -join "`n")
        return [pscustomobject]@{ Clean = $clean; Text = $text; Thrown = $thrown }
    }

    # A server that shuts down when asked: a clean stop, and the token went with the request.
    $server = New-Sleeper
    $launchOk = New-LaunchId
    $fake = Start-StopServer -Health ('{"status": "ok", "launch": "' + $launchOk + '"}') -Process $server -Obey
    try {
        $case = Invoke-StopCase $server $fake.Port $launchOk
        $requests = @($fake.Seen)
    }
    finally {
        Stop-FakeServer $fake
        if (-not $server.HasExited) { $server.Kill() }
    }
    Assert-That 'a server that shuts down when asked is a clean stop' ($case.Clean -eq $true)
    Assert-That 'and nothing about a forced end is printed' ($case.Text -notmatch 'did not stop cleanly')
    Assert-That 'and its record is removed once it has exited' (-not (Test-Path -LiteralPath $script:RecordFile))
    Assert-That 'the identity is checked before the shutdown is asked' `
        (($requests.Count -eq 2) -and ($requests[0] -match '\AGET /api/health ') -and ($requests[1] -match '\APOST /api/shutdown '))
    Assert-That 'the shutdown request carries the token from a vault folder with non-ASCII letters' `
        ($requests[1] -cmatch ('(?m)^Authorization: Bearer ' + [regex]::Escape($stopToken) + '\r?$'))
    Assert-That "and the Origin of the server's own address" `
        ($requests[1] -cmatch ('(?m)^Origin: http://127\.0\.0\.1:' + $fake.Port + '\r?$'))
    Assert-That 'the health check does not carry the token' ($requests[0] -notmatch [regex]::Escape($stopToken))

    # The server takes the request but does not exit: the forced end, said so.
    $server = New-Sleeper
    $fake = Start-StopServer -Health ('{"status": "ok", "launch": "' + $launchOk + '"}') -Process $server
    try {
        $case = Invoke-StopCase $server $fake.Port $launchOk -Seconds 1
    }
    finally {
        Stop-FakeServer $fake
    }
    Assert-That 'a server that ignores the request is ended' ($server.HasExited -and ($case.Clean -eq $false))
    Assert-That 'and stop says it did not stop cleanly, and why' `
        (($case.Text -match 'did not stop cleanly') -and ($case.Text -match 'did not exit within 1 seconds'))
    Assert-That 'and its record is removed once it has been ended' (-not (Test-Path -LiteralPath $script:RecordFile))
    Assert-That 'and nothing printed holds the token or the vault folder' `
        ((-not $case.Text.Contains($stopToken)) -and (-not $case.Text.Contains($stopVault)))
    if (-not $server.HasExited) { $server.Kill() }

    # The request fails: refused by the server, or nothing answers on the address at all.
    foreach ($status in 401, 403, 500, 503) {
        $server = New-Sleeper
        $fake = Start-StopServer -Health ('{"status": "ok", "launch": "' + $launchOk + '"}') -Status $status -Process $server -Obey
        try {
            $case = Invoke-StopCase $server $fake.Port $launchOk
        }
        finally {
            Stop-FakeServer $fake
        }
        Assert-That "a refused shutdown request (HTTP $status) falls back to ending the server" `
            ($server.HasExited -and ($case.Clean -eq $false) -and ($case.Text -match "refused \(HTTP $status\)"))
        Assert-That "and its record is removed (HTTP $status)" (-not (Test-Path -LiteralPath $script:RecordFile))
        if (-not $server.HasExited) { $server.Kill() }
    }
    $server = New-Sleeper
    $case = Invoke-StopCase $server $taken $launchOk   # nothing listens on $taken any more
    Assert-That 'a server that does not answer on its address is ended' `
        ($server.HasExited -and ($case.Clean -eq $false) -and ($case.Text -match 'does not answer'))
    Assert-That 'and its record is removed' (-not (Test-Path -LiteralPath $script:RecordFile))
    if (-not $server.HasExited) { $server.Kill() }

    # Another program on the address: it never receives the token, and the recorded process is ended.
    foreach ($health in @(
            @{ name = 'another launch id'; body = '{"status": "ok", "launch": "' + (New-LaunchId) + '"}' },
            @{ name = 'the id in another case'; body = '{"status": "ok", "launch": "' + $launchOk.ToUpperInvariant() + '"}' },
            @{ name = 'no launch id'; body = '{"status": "ok", "launch": null}' },
            @{ name = 'no launch field'; body = '{"status": "ok"}' },
            @{ name = 'text that is not JSON'; body = 'hello' }
        )) {
        $server = New-Sleeper
        $fake = Start-StopServer -Health $health.body -Process $server -Obey
        try {
            $case = Invoke-StopCase $server $fake.Port $launchOk
            $requests = @($fake.Seen)
        }
        finally {
            Stop-FakeServer $fake
        }
        Assert-That "an answer with $($health.name) is not asked to shut down" `
            ((@($requests | Where-Object { $_ -match '\APOST ' }).Count -eq 0) -and (@($requests | Where-Object { $_.Contains($stopToken) }).Count -eq 0))
        Assert-That "and the recorded server is ended instead ($($health.name))" `
            ($server.HasExited -and ($case.Clean -eq $false) -and ($case.Text -match 'another program answers'))
        if (-not $server.HasExited) { $server.Kill() }
    }

    # A record from an older launcher (no launch id) or a vault folder without its token: no
    # request is made, and the server is ended.
    $server = New-Sleeper
    $fake = Start-StopServer -Health ('{"status": "ok", "launch": "' + $launchOk + '"}') -Process $server -Obey
    try {
        $case = Invoke-StopCase $server $fake.Port ''
        $requests = @($fake.Seen)
    }
    finally {
        Stop-FakeServer $fake
    }
    Assert-That 'a record without a launch id makes no request' ($requests.Count -eq 0)
    Assert-That 'and ends the server, saying why' `
        ($server.HasExited -and ($case.Clean -eq $false) -and ($case.Text -match 'older launcher'))
    if (-not $server.HasExited) { $server.Kill() }
    $server = New-Sleeper
    $emptyVault = Join-Path $work ('no-token-' + [char]0x00DF)
    New-Item -ItemType Directory -Path $emptyVault | Out-Null
    $fake = Start-StopServer -Health ('{"status": "ok", "launch": "' + $launchOk + '"}') -Process $server -Obey
    try {
        $case = Invoke-StopCase $server $fake.Port $launchOk -Vault $emptyVault
        $requests = @($fake.Seen)
    }
    finally {
        Stop-FakeServer $fake
    }
    Assert-That 'a vault folder without its token makes no shutdown request' `
        (@($requests | Where-Object { $_ -match '\APOST ' }).Count -eq 0)
    Assert-That 'and ends the server, saying why without the folder' `
        ($server.HasExited -and ($case.Clean -eq $false) -and ($case.Text -match 'token could not be read') -and (-not $case.Text.Contains($emptyVault)))
    if (-not $server.HasExited) { $server.Kill() }

    # Neither way works: the request fails and the process refuses to end. The record is kept.
    $stubbornServer = [pscustomobject]@{ Id = 2147480000; HasExited = $false; StartTime = (Get-Date) }
    $stubbornServer | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) $false }
    Write-Record $self $facts
    $running = [pscustomobject]@{ Process = $stubbornServer; Host = '127.0.0.1'; Port = $taken; Vault = $stopVault; Launch = $launchOk }
    $message = Get-ThrownMessage { Stop-Server $running -Seconds 1 6>$null }
    Assert-That 'a server that cannot be asked or ended is reported' (($null -ne $message) -and ($message -match 'could not be stopped'))
    Assert-That 'and the record is kept, so stop can be tried again' (Test-Path -LiteralPath $script:RecordFile)
    # It takes the request, says it exited, but still runs: the record is not removed on its word.
    $liar = [pscustomobject]@{ Id = 2147480000; HasExited = $false; StartTime = (Get-Date) }
    $liar | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) $true }
    $fake = Start-StopServer -Health ('{"status": "ok", "launch": "' + $launchOk + '"}')
    try {
        $running = [pscustomobject]@{ Process = $liar; Host = '127.0.0.1'; Port = $fake.Port; Vault = $stopVault; Launch = $launchOk }
        $message = Get-ThrownMessage { Stop-Server $running -Seconds 1 6>$null }
    }
    finally {
        Stop-FakeServer $fake
    }
    Assert-That 'a process that has not exited is not taken for stopped' (($null -ne $message) -and ($message -match 'could not be stopped'))
    Assert-That 'and its record is kept' (Test-Path -LiteralPath $script:RecordFile)
    Remove-Record

    # A stale record: stop says the server is not running and removes it, sending nothing.
    Write-Record $self ([pscustomobject]@{ host = '127.0.0.1'; port = $taken; vault = $stopVault }) $launchOk
    $record = Read-TextFile $script:RecordFile | ConvertFrom-Json
    $record.pid = 2147480000
    Write-TextFile $script:RecordFile ($record | ConvertTo-Json)
    $said = (@(Invoke-Stop 6>&1 | ForEach-Object { [string]$_ }) -join "`n")
    Assert-That 'stop with a stale record says the server is not running' ($said -match 'not running')
    Assert-That 'and removes the stale record' (-not (Test-Path -LiteralPath $script:RecordFile))

    # The whole command: stop asks, the server exits, stop says it stopped cleanly.
    $server = New-Sleeper
    $fake = Start-StopServer -Health ('{"status": "ok", "launch": "' + $launchOk + '"}') -Process $server -Obey
    try {
        Write-Record $server ([pscustomobject]@{ host = '127.0.0.1'; port = $fake.Port; vault = $stopVault }) $launchOk
        $said = (@(Invoke-Stop 6>&1 | ForEach-Object { [string]$_ }) -join "`n")
    }
    finally {
        Stop-FakeServer $fake
        if (-not $server.HasExited) { $server.Kill() }
    }
    Assert-That 'stop says the server stopped cleanly' ($said -match 'stopped cleanly')
    Assert-That 'and has removed its record' (-not (Test-Path -LiteralPath $script:RecordFile))

    # --- the lock around check-and-start -------------------------------------------------------
    $name = Get-LockName
    $probe = {
        param($lockName)
        $m = New-Object System.Threading.Mutex($false, $lockName)
        try {
            if ($m.WaitOne(0)) { $m.ReleaseMutex(); return $true }
            return $false
        }
        finally { $m.Dispose() }
    }
    # BeginInvoke, so the probe runs on a thread of its own: a mutex is owned by a thread, and the
    # thread that holds it is let in again, which would make "held" look "free".
    $freeFromElsewhere = {
        $shell = [powershell]::Create().AddScript($probe).AddArgument($name)
        try { return [bool]($shell.EndInvoke($shell.BeginInvoke()) | Select-Object -First 1) }
        finally { $shell.Dispose() }
    }
    Assert-That 'the lock is free when nobody holds it' (& $freeFromElsewhere)
    $duringBody = $null
    $returned = Invoke-Locked { $script:duringBody = (& $freeFromElsewhere); 'value' }
    Assert-That 'the lock is held while the block runs' ($script:duringBody -eq $false)
    Assert-That 'the block can hand a value back' ($returned -ceq 'value')
    Assert-That 'the lock is released afterwards' (& $freeFromElsewhere)
    $null = Get-ThrownMessage { Invoke-Locked { throw 'boom' } }
    Assert-That 'the lock is released when the block fails' (& $freeFromElsewhere)

    # Another thread holds the lock: a command waits its turn, and gives up with a plain message.
    $held = New-Object System.Threading.ManualResetEvent($false)
    $release = New-Object System.Threading.ManualResetEvent($false)
    $holder = [powershell]::Create().AddScript({
            param($lockName, $held, $release)
            $m = New-Object System.Threading.Mutex($false, $lockName)
            $null = $m.WaitOne()
            $null = $held.Set()
            $null = $release.WaitOne()
            $m.ReleaseMutex()
            $m.Dispose()
        }).AddArgument($name).AddArgument($held).AddArgument($release)
    $pending = $holder.BeginInvoke()
    try {
        Assert-That 'the holder got the lock' ($held.WaitOne(10000))
        $ran = $false
        $message = Get-ThrownMessage { Invoke-Locked -WaitSeconds 1 { $script:ran = $true } }
        Assert-That 'a command that cannot get the lock gives up' (($null -ne $message) -and ($message -match 'another vault.ps1 command'))
        Assert-That 'and does not run its block' (-not $script:ran)
    }
    finally {
        $null = $release.Set()
        $null = $holder.EndInvoke($pending)
        $holder.Dispose()
    }
    Assert-That 'the lock is free again once the holder is done' (& $freeFromElsewhere)

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

# --- check: the backend's exit code becomes the launcher's ---------------------------------------
Assert-That 'a clean check is no error' ($null -eq (Get-CheckVerdict 0))
$verdict = Get-CheckVerdict 1
Assert-That 'a check that found problems stops with a message' (($null -ne $verdict) -and ($verdict -match 'found problems'))
$verdict = Get-CheckVerdict 2
Assert-That 'a check that could not run says so' (($null -ne $verdict) -and ($verdict -match 'could not run'))
$commandParameter = (Get-Command $launcher).Parameters['Command']
$allowed = @($commandParameter.Attributes | Where-Object { $_ -is [System.Management.Automation.ValidateSetAttribute] })[0].ValidValues
Assert-That 'check is one of the commands' ($allowed -contains 'check')

# A stand-in for the backend's Python: it records its arguments and sets the exit code asked for.
# That the real interpreter's exit code reaches the launcher is the CI job's end-to-end step.
# The tests above leave BackendDir pointing at a folder of the removed work folder, so check
# gets a backend folder of its own.
$checkDir = Join-Path ([System.IO.Path]::GetTempPath()) ('vault-check-tests-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $checkDir
$script:BackendDir = $checkDir
$script:checkArguments = $null
$script:checkFolder = $null
$script:checkCode = 0
$standIn = {
    $script:checkArguments = $args -join ' '
    $script:checkFolder = (Get-Location).Path
    $global:LASTEXITCODE = $script:checkCode
}
$startedIn = (Get-Location).Path
$message = Get-ThrownMessage { Invoke-Check -Python $standIn }
if ($null -ne $message) { Write-Host "     (check said: $message)" }
Assert-That 'check passes when the backend finds nothing' ($null -eq $message)
Assert-That 'check runs the backend module vault.check with no other argument' ($script:checkArguments -ceq '-m vault.check')
Assert-That 'check runs in the backend folder' ($script:checkFolder -eq $checkDir)
Assert-That 'check returns to the folder it was called from' ((Get-Location).Path -eq $startedIn)
$script:checkCode = 1
$message = Get-ThrownMessage { Invoke-Check -Python $standIn }
Assert-That 'check fails when the backend finds a problem' (($null -ne $message) -and ($message -match 'found problems'))
$script:checkCode = 2
$message = Get-ThrownMessage { Invoke-Check -Python $standIn }
Assert-That 'check fails when the backend cannot run' (($null -ne $message) -and ($message -match 'could not run'))
Assert-That 'and still returns to the folder it was called from' ((Get-Location).Path -eq $startedIn)
Remove-Item -LiteralPath $checkDir -Recurse -Force -ErrorAction SilentlyContinue

# --- backup: the folder reaches the backend in the environment, never on its command line ------
$message = Get-ThrownMessage { Invoke-Backup '' }
Assert-That 'backup without a folder says how to name one' (($null -ne $message) -and ($message -match '-To'))
# A stand-in for the backend's Python, run in this process: it records its arguments, the folder
# it was handed and where it ran, prints a line and sets the exit code asked for. No batch file
# or file read is involved, so nothing here can end the test run early. That the real
# interpreter's exit code reaches the launcher is the CI job's end-to-end step. The tests above
# leave BackendDir pointing into the removed work folder, so backup gets a folder of its own.
$backupDir = Join-Path ([System.IO.Path]::GetTempPath()) ('vault-backup-tests-' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $backupDir
$script:BackendDir = $backupDir
$script:backupArguments = $null
$script:backupTarget = $null
$script:backupFolder = $null
$script:backupCode = 0
$backupStandIn = {
    $script:backupArguments = $args -join ' '
    $script:backupTarget = [Environment]::GetEnvironmentVariable('VAULT_BACKUP_TO', 'Process')
    $script:backupFolder = (Get-Location).Path
    Write-Output 'line from the stand-in'
    $global:LASTEXITCODE = $script:backupCode
}
function Invoke-BackupLines {
    <# Run backup with the stand-in and return its printed lines, or $null if it threw (the
       message is shown), so a failure is a failed check and not the end of the run. #>
    param([string]$Target)
    try {
        return , @(Invoke-Backup $Target -Python $backupStandIn 6>&1 | ForEach-Object { [string]$_ })
    }
    catch {
        Write-Host "     (backup said: $($_.Exception.Message))"
        return $null
    }
}
try {
    $relativeBase = Join-Path $backupDir 'here'
    $null = New-Item -ItemType Directory -Path $relativeBase
    Push-Location -LiteralPath $relativeBase
    try {
        $lines = Invoke-BackupLines 'my backups'
    }
    finally {
        Pop-Location
    }
    Assert-That 'backup passes when the backend succeeds' ($null -ne $lines)
    Assert-That 'a relative backup folder is taken from the current location' `
        ($script:backupTarget -ceq (Join-Path $relativeBase 'my backups'))
    Assert-That 'the folder is not on the backend command line' ($script:backupArguments -ceq '-m vault.backup')
    Assert-That 'backup runs in the backend folder' ($script:backupFolder -eq $backupDir)
    Assert-That "the backend's lines are passed on" (($null -ne $lines) -and ($lines -contains 'line from the stand-in'))
    $startedIn = (Get-Location).Path
    $spaced = Join-Path $backupDir 'My Backups'
    $lines = Invoke-BackupLines ($spaced + '\')
    $shape = '\A' + [regex]::Escape($spaced) + '\\?\z'
    Assert-That 'a folder with a space and a trailing backslash arrives intact' `
        (($script:backupTarget -is [string]) -and ($script:backupTarget -match $shape))
    Assert-That 'the folder is taken out of the environment afterwards' `
        ($null -eq [Environment]::GetEnvironmentVariable('VAULT_BACKUP_TO', 'Process'))
    Assert-That 'backup returns to the folder it was called from' ((Get-Location).Path -eq $startedIn)
    $script:backupCode = 1
    $message = Get-ThrownMessage { Invoke-Backup (Join-Path $backupDir 'VaultBackups') -Python $backupStandIn 6>&1 }
    Assert-That 'a backup that fails stops with its exit code' (($null -ne $message) -and ($message -match 'exit code 1'))
    Assert-That 'and does not repeat the folder' (($null -ne $message) -and (-not $message.Contains('VaultBackups')))
    Assert-That 'and takes the folder out of the environment too' `
        ($null -eq [Environment]::GetEnvironmentVariable('VAULT_BACKUP_TO', 'Process'))
    Assert-That 'and still returns to the folder it was called from' ((Get-Location).Path -eq $startedIn)
}
finally {
    Remove-Item -LiteralPath $backupDir -Recurse -Force -ErrorAction SilentlyContinue
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
$ErrorActionPreference = 'Continue'
$null = & $shell -NoProfile -File $launcher 'backup' 2>&1
$backupExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
Assert-That 'backup without -To is refused' ($backupExit -eq 1)

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
