<#
.SYNOPSIS
    Install, run, stop and develop AI Document Vault on Windows.

.DESCRIPTION
    .\vault.ps1 install   Sync the Python packages from the lock file, install the frontend
                          packages and build the frontend.
    .\vault.ps1 run       Start the server in the background and open the app in the browser.
    .\vault.ps1 stop      Stop the server that "run" started.
    .\vault.ps1 dev       Start the server in the background and the Vite dev server in this
                          window; the server stops when the dev server does.

    -NoBrowser            Do not open the browser (run and dev).

    The settings come from backend\config.toml (copy config.example.toml there and set the vault
    folder) or from the file named by the AIVAULT_CONFIG environment variable.

    The token the app needs (ADR 0016) is read from the vault folder and handed to the page in the
    address fragment. It is never printed. It is visible to local programs for a moment as part of
    the browser's command line, a limit that ADR 0016 accepts. The browser is opened only once the
    server (or, for dev, Vite) answers with the random launch id it was started with, so the token
    never goes to another program that holds the port.

    Works in Windows PowerShell 5.1 and PowerShell 7.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('install', 'run', 'stop', 'dev')]
    [string]$Command,

    [switch]$NoBrowser
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:Root = $PSScriptRoot
$script:BackendDir = Join-Path $script:Root 'backend'
$script:FrontendDir = Join-Path $script:Root 'frontend'
# Where the launcher keeps its own state. Nothing in it is private: a process id and the server's
# own logs, which the backend writes without document content. The folder is git-ignored.
$script:RunDir = Join-Path $script:Root '.run'
$script:RecordFile = Join-Path $script:RunDir 'server.json'
$script:DevPort = 5173
# How long the server may take to start before the launcher gives up on it. The first start of an
# existing vault checks the access list of every file below it, which the backend allows 15 minutes
# (TREE_TIMEOUT_SECONDS in backend/src/vault/storage/acl.py), before its other checks and the
# database migration. A server that exits is noticed at once; this only bounds one that hangs.
# A backend test keeps this above the backend's own bound.
$script:StartupMinutes = 20

# \A and \z, not ^ and $: in .NET (as in Python) $ also matches before a trailing newline.
$script:TokenShape = '\A[A-Za-z0-9_-]{32,128}\z'
# The environment variable that carries a start's launch id to the server and to Vite; see
# backend/src/vault/api/health.py and frontend/vite.config.ts.
$script:LaunchVariable = 'VAULT_LAUNCH_ID'


function Test-TokenShape {
    param([AllowNull()][object]$Token)
    return ($Token -is [string]) -and ($Token -match $script:TokenShape)
}

function Get-AppAddress {
    <# The address the browser opens: the token goes in the fragment, which is never sent to a
       server (ADR 0016). The value is never echoed if it is wrong. #>
    param(
        [Parameter(Mandatory)][string]$HostName,
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$Token
    )
    if (-not (Test-TokenShape $Token)) {
        throw 'the token is not in the expected shape; delete the file local-token in the vault folder to create a new one'
    }
    if ($HostName.Contains(':')) { $HostName = "[$HostName]" }
    return "http://${HostName}:${Port}/#token=${Token}"
}

function Get-BackendPython {
    $python = Join-Path $script:BackendDir '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'the backend is not installed yet; run .\vault.ps1 install first'
    }
    return $python
}

function Assert-Tool {
    param([string]$Name, [string]$Hint)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is not installed. $Hint"
    }
}

function Invoke-Native {
    <# Run a program in a folder and stop if it fails. #>
    param([string]$File, [string[]]$Arguments, [string]$WorkingDirectory)
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $File @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$File $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

function Get-VaultFacts {
    <# Ask the backend itself where the vault is and which port it uses, so the launcher never
       parses config.toml and cannot disagree with the server about either. #>
    $python = Get-BackendPython
    $code = @'
import json
from vault.core.config import load_settings

settings = load_settings()
# ensure_ascii: a path with an accented letter reaches PowerShell as \u00e9, not as bytes that
# the console code page would decode wrongly.
print(json.dumps({
    "vault": None if settings.vault.path is None else str(settings.vault.path),
    "host": settings.server.host,
    "port": settings.server.port,
}, ensure_ascii=True))
'@
    Push-Location -LiteralPath $script:BackendDir
    try {
        $lines = @($code | & $python -)
        if ($LASTEXITCODE -ne 0) {
            throw 'the settings could not be read (see the message above); check backend\config.toml or AIVAULT_CONFIG'
        }
    }
    finally {
        Pop-Location
    }
    $facts = $lines[-1] | ConvertFrom-Json
    if (-not $facts.vault) {
        throw 'no vault folder is set: copy config.example.toml to backend\config.toml and set vault.path (or point AIVAULT_CONFIG at your file)'
    }
    return $facts
}

function Read-Token {
    param([Parameter(Mandatory)][string]$VaultFolder)
    $file = Join-Path $VaultFolder 'local-token'
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
        throw 'the token file local-token is not in the vault folder; the server creates it when it starts'
    }
    $token = (Get-Content -LiteralPath $file -Raw).Trim()
    if (-not (Test-TokenShape $token)) {
        throw 'the file local-token does not hold a token; delete it and start the server again to create a new one'
    }
    return $token
}

function Get-StartStamp {
    <# The start time as a number of ticks. Not a date string: PowerShell 7 turns a date-like
       string into a date when it reads JSON, which would then never compare equal. #>
    param([System.Diagnostics.Process]$Process)
    return [int64]$Process.StartTime.ToUniversalTime().Ticks
}

function Write-TextFile {
    <# UTF-8 without a byte order mark, in both editions. The record holds the vault folder, and
       Windows folder names are not always ASCII (an accented letter in a user name): "-Encoding ASCII" writes a
       "?" for each such letter, and Windows PowerShell 5.1 reads a file without a mark as the
       ANSI code page, so the record is read back with Read-TextFile. The path is absolute, as
       .NET resolves a relative one against the process folder, not the PowerShell location. #>
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Read-TextFile {
    param([Parameter(Mandatory)][string]$Path)
    return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
}

function Write-Record {
    <# Remember the server: its process id and start time, and the settings it was started with, so
       a later "run" talks to the server that is up and not to whatever the settings say now. #>
    param([System.Diagnostics.Process]$Process, [object]$Facts)
    New-Item -ItemType Directory -Force -Path $script:RunDir | Out-Null
    $record = [ordered]@{
        pid     = $Process.Id
        started = (Get-StartStamp $Process)
        host    = [string]$Facts.host
        port    = [int]$Facts.port
        vault   = [string]$Facts.vault
    }
    Write-TextFile $script:RecordFile ($record | ConvertTo-Json)
}

function Remove-Record {
    <# Forget the server. Given a process, only if the record still names that process (and that
       start time, when it is known): "stop" can end the server of a "dev" that is still open, and
       a later "run" then starts another and writes its record, which dev must not remove when it
       closes, or that server would run where "stop" cannot find it. #>
    param([object]$Process, [object]$Started)
    if ($null -ne $Process) {
        try {
            $record = Read-TextFile $script:RecordFile | ConvertFrom-Json
            $mine = ([int]$record.pid -eq [int]$Process.Id) -and
                (($null -eq $Started) -or ([int64]$record.started -eq [int64]$Started))
        }
        catch {
            return   # no record, or one that Get-RunningServer will clear
        }
        if (-not $mine) { return }
    }
    Remove-Item -LiteralPath $script:RecordFile -Force -ErrorAction SilentlyContinue
}

function Get-RunningServer {
    <# The server the last "run" started, or $null: its Process and the settings it was started with
       (Host, Port and Vault, each $null if the record lacks it). The record holds the process id
       and its start time, so a process that only reuses the number is never taken for the server.
       A record that cannot be read, or that names a process that has ended, is removed. #>
    if (-not (Test-Path -LiteralPath $script:RecordFile -PathType Leaf)) { return $null }
    try {
        $record = Read-TextFile $script:RecordFile | ConvertFrom-Json
        $process = Get-Process -Id ([int]$record.pid) -ErrorAction Stop
        if ((Get-StartStamp $process) -eq [int64]$record.started) {
            $names = @($record.PSObject.Properties.Name)
            $recordedHost = $null
            $recordedPort = $null
            $recordedVault = $null
            if (($names -contains 'host') -and ($record.host -is [string]) -and $record.host) {
                $recordedHost = $record.host
            }
            if (($names -contains 'port') -and ($record.port -is [ValueType]) -and ($record.port -isnot [bool]) -and ([int]$record.port -ge 1) -and ([int]$record.port -le 65535)) {
                $recordedPort = [int]$record.port
            }
            if (($names -contains 'vault') -and ($record.vault -is [string]) -and $record.vault) {
                $recordedVault = $record.vault
            }
            return [pscustomobject]@{
                Process = $process
                Host    = $recordedHost
                Port    = $recordedPort
                Vault   = $recordedVault
            }
        }
    }
    catch {
        # unreadable, or no such process: stale either way
    }
    Remove-Record
    return $null
}

function Test-PortInUse {
    <# True if something accepts connections on the address. The server's own readiness check
       is "something answers /api/health", which another copy of the app on the same port would
       satisfy for a child that is about to fail to bind, so a taken port is refused before
       anything is started. (A wildcard address cannot be connected to, so it is probed on
       loopback.) A probe that cannot run counts as free: the start-up then reports what it finds. #>
    param([Parameter(Mandatory)][string]$HostName, [Parameter(Mandatory)][int]$Port)
    if ($HostName -eq '0.0.0.0') { $HostName = '127.0.0.1' }
    if ($HostName -eq '::') { $HostName = '::1' }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(2000)) { return $false }
        try {
            $client.EndConnect($pending)
            return $true
        }
        catch {
            return $false   # refused: nothing listens
        }
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Get-LockName {
    <# One lock per checkout (its state folder), so two copies of the app are not made to wait
       for each other. The name is a hash because a mutex name cannot hold backslashes. #>
    $hash = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $hash.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($script:RunDir.ToLowerInvariant()))
    }
    finally {
        $hash.Dispose()
    }
    return 'Local\AIVaultLauncher-' + ([BitConverter]::ToString($bytes) -replace '-', '').Substring(0, 32)
}

function Invoke-Locked {
    <# Run a block while holding the launcher's lock. Two commands started together would both
       see "no server", both start one, and the one that fails to bind would delete the record of
       the one that works, leaving a server that "stop" cannot find. A holder that was killed
       leaves the lock abandoned, which the next command simply takes over. #>
    param([Parameter(Mandatory)][scriptblock]$Body, [int]$WaitSeconds = 120)
    $mutex = New-Object System.Threading.Mutex($false, (Get-LockName))
    $held = $false
    try {
        try {
            $held = $mutex.WaitOne([TimeSpan]::FromSeconds($WaitSeconds))
        }
        catch [System.Threading.AbandonedMutexException] {
            $held = $true
        }
        if (-not $held) {
            throw 'another vault.ps1 command is still starting or stopping the server; wait for it to finish and try again'
        }
        & $Body
    }
    finally {
        if ($held) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

function New-LaunchId {
    <# 32 random bytes as base64url (43 characters, the token's alphabet). #>
    $bytes = New-Object byte[] 32
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($bytes)
    }
    finally {
        $random.Dispose()
    }
    return ([Convert]::ToBase64String($bytes).TrimEnd('=') -replace '\+', '-' -replace '/', '_')
}

function Start-WithLaunchId {
    <# Run a block that starts a process with the launch id in its environment, and take the id
       out of this process's environment again afterwards, whatever happens. #>
    param([Parameter(Mandatory)][string]$Launch, [Parameter(Mandatory)][scriptblock]$Body)
    $previous = [Environment]::GetEnvironmentVariable($script:LaunchVariable, 'Process')
    [Environment]::SetEnvironmentVariable($script:LaunchVariable, $Launch, 'Process')
    try {
        & $Body
    }
    finally {
        [Environment]::SetEnvironmentVariable($script:LaunchVariable, $previous, 'Process')
    }
}

function Wait-Launch {
    <# Wait until the address answers with the launch id the process was started with. "Something
       answers" is not enough: the port is checked before the process starts, but another program
       (another Windows account's, too) can take it while the process is still starting, which for
       the server can be many minutes, and the browser would then hand it the vault's token. No
       other program can read the environment of the process started here, so the id proves
       which one answered. #>
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][string]$Launch,
        [Parameter(Mandatory)][object]$Process,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Hint,
        [Parameter(Mandatory)][double]$Minutes,
        [string]$SlowNote
    )
    $began = Get-Date
    $deadline = $began.AddMinutes($Minutes)
    $told = $false
    while ($true) {
        if ($Process.HasExited) {
            throw "the $Name stopped while starting; $Hint"
        }
        $body = $null
        try {
            $answer = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 2
            if ($answer.StatusCode -eq 200) {
                $body = $answer.Content
                if ($body -is [byte[]]) { $body = [System.Text.Encoding]::UTF8.GetString($body) }
            }
        }
        catch {
            # not listening yet
        }
        if ($null -ne $body) {
            $id = $null
            try {
                $id = ([string]$body | ConvertFrom-Json).launch
            }
            catch {
                # not JSON, or no launch id in it
            }
            if (($id -is [string]) -and ($id -ceq $Launch)) { return }
            throw "another program answers on the $Name's address, which it took while the $Name was starting; the browser was not opened and the $Name is stopped"
        }
        if ((Get-Date) -gt $deadline) {
            throw "the $Name did not answer within $Minutes minutes; $Hint"
        }
        if ($SlowNote -and -not $told -and ((Get-Date) - $began).TotalSeconds -gt 10) {
            Write-Host $SlowNote
            $told = $true
        }
        Start-Sleep -Milliseconds 500
    }
}

function Start-Backend {
    <# Start the server hidden, remember it and wait until it answers with its launch id. #>
    param([Parameter(Mandatory)][object]$Facts)
    $HostName = [string]$Facts.host
    $Port = [int]$Facts.port
    if (Test-PortInUse $HostName $Port) {
        throw "something already answers on ${HostName}:${Port} (another copy of the app, or another program); stop it, or set another server.port, before starting the server"
    }
    $python = Get-BackendPython
    New-Item -ItemType Directory -Force -Path $script:RunDir | Out-Null
    $launch = New-LaunchId
    $process = Start-WithLaunchId $launch {
        Start-Process -FilePath $python -ArgumentList @('-m', 'vault') `
            -WorkingDirectory $script:BackendDir -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $script:RunDir 'server.out.log') `
            -RedirectStandardError (Join-Path $script:RunDir 'server.err.log')
    }
    # Whatever stops start-up from completing (a record that cannot be written, a server that
    # exits or never answers, Ctrl+C) must not leave a server running that "stop" cannot find.
    $ready = $false
    try {
        Write-Record $process $Facts
        $url = "http://${HostName}:${Port}/api/health"
        if ($HostName.Contains(':')) { $url = "http://[${HostName}]:${Port}/api/health" }
        Wait-Launch -Url $url -Launch $launch -Process $process -Name 'server' `
            -Hint 'see .run\server.err.log' -Minutes $script:StartupMinutes `
            -SlowNote 'Still starting. The first start of a large vault checks the access list of every file in it, which can take several minutes.'
        $ready = $true
        return $process
    }
    finally {
        if (-not $ready) {
            try {
                Stop-Backend $process
            }
            catch {
                # The start-up error keeps travelling; this says why the server is still there.
                [Console]::Error.WriteLine("vault: $($_.Exception.Message)")
            }
        }
    }
}

function Stop-Backend {
    <# End the server and forget it. The record stays if the process could not be ended, so
       that "stop" can be tried again and the launcher never reports a server that is still
       holding the port as stopped. #>
    param([Parameter(Mandatory)][object]$Process, [object]$Started)
    if ($null -eq $Started) {
        # Read before the process is ended; an ended one may no longer say when it started.
        try { $Started = Get-StartStamp $Process } catch { $Started = $null }
    }
    if (-not $Process.HasExited) {
        try {
            Stop-Process -Id $Process.Id -Force -ErrorAction Stop
        }
        catch {
            # It may have ended on its own just now; otherwise it was refused.
            if (-not $Process.HasExited) {
                throw 'the server could not be stopped (access was refused, or it keeps running); its record is kept, so run .\vault.ps1 stop to try again'
            }
        }
        if (-not $Process.WaitForExit(10000)) {
            throw 'the server did not exit within 10 seconds; its record is kept, so run .\vault.ps1 stop to try again'
        }
    }
    Remove-Record -Process $Process -Started $Started
}

function Open-App {
    param([string]$Address)
    if ($NoBrowser) { return }
    Start-Process $Address
}

function Invoke-Install {
    Assert-Tool 'uv' 'Install it with: winget install --id astral-sh.uv   (https://docs.astral.sh/uv/)'
    Assert-Tool 'node' 'Install Node 22.13 or newer: winget install OpenJS.NodeJS.LTS'
    Assert-Tool 'npm' 'It comes with Node.'
    Write-Host 'Installing the backend packages from the lock file...'
    Invoke-Native 'uv' @('sync', '--locked') $script:BackendDir
    Write-Host 'Installing the frontend packages from the lock file...'
    Invoke-Native 'npm' @('ci') $script:FrontendDir
    Write-Host 'Building the frontend...'
    Invoke-Native 'npm' @('run', 'build') $script:FrontendDir
    Write-Host 'Installed. Copy config.example.toml to backend\config.toml, set vault.path, then run .\vault.ps1 run'
}

function Get-OrStartServer {
    <# The settings of the server that is running, starting one if there is none. #>
    $running = Get-RunningServer
    if ($running) {
        # Talk to the server that is up, whatever the settings say now.
        if (-not ($running.Host -and $running.Port -and $running.Vault)) {
            throw 'the running server was started by an older launcher and its settings are not recorded; stop it with .\vault.ps1 stop and run again'
        }
        $facts = [pscustomobject]@{ host = $running.Host; port = $running.Port; vault = $running.Vault }
        Write-Host 'The server is already running.'
        try {
            $now = Get-VaultFacts
            if (($now.host -ne $facts.host) -or ($now.port -ne $facts.port) -or ($now.vault -ne $facts.vault)) {
                Write-Host 'The settings have changed since it started. Stop it with .\vault.ps1 stop and run again to use them.'
            }
        }
        catch {
            Write-Host 'The settings cannot be read now; opening the server that is running.'
        }
        return $facts
    }
    $facts = Get-VaultFacts
    $null = Start-Backend $facts
    return $facts
}

function Invoke-Run {
    $facts = Invoke-Locked { Get-OrStartServer }
    $token = Read-Token $facts.vault
    $address = Get-AppAddress -HostName $facts.host -Port $facts.port -Token $token
    Open-App $address
    Write-Host "The app is at http://$($facts.host):$($facts.port)/ (the browser is opened with its token). Stop it with .\vault.ps1 stop"
}

function Invoke-Stop {
    Invoke-Locked {
        $running = Get-RunningServer
        if (-not $running) {
            Write-Host 'The server is not running.'
            return
        }
        Stop-Backend $running.Process
        Write-Host 'The server is stopped.'
    }
}

function Invoke-Dev {
    Assert-Tool 'node' 'Install Node 22.13 or newer: winget install OpenJS.NodeJS.LTS'
    $vite = Join-Path $script:FrontendDir 'node_modules\vite\bin\vite.js'
    if (-not (Test-Path -LiteralPath $vite -PathType Leaf)) {
        throw 'the frontend is not installed yet; run .\vault.ps1 install first'
    }
    $facts = Get-VaultFacts
    if ($facts.port -ne 8765 -or $facts.host -ne '127.0.0.1') {
        # frontend/vite.config.ts forwards /api to http://127.0.0.1:8765, so the server has to be
        # there. "localhost" is not enough: it can resolve to ::1.
        throw "dev expects server.host 127.0.0.1 and server.port 8765 (the Vite proxy is fixed to that address), but the settings say $($facts.host) and $($facts.port)"
    }
    # Refused before anything starts; Wait-Launch catches a program that takes it later.
    if (Test-PortInUse '127.0.0.1' $script:DevPort) {
        throw "something already answers on 127.0.0.1:$($script:DevPort) (another dev server, or another program); stop it before starting dev"
    }
    $server = Invoke-Locked {
        if (Get-RunningServer) {
            throw 'a server started by "run" is already running; stop it first with .\vault.ps1 stop'
        }
        Start-Backend $facts
    }
    # Taken while the server is surely running: an ended process may no longer say when it started.
    $serverStarted = Get-StartStamp $server
    $dev = $null
    try {
        $launch = New-LaunchId
        # Node runs Vite directly, in this window: Ctrl+C reaches it, and no cmd.exe asks
        # "Terminate batch job?". The script path is relative, as Windows PowerShell 5.1 does not
        # quote the arguments it passes.
        $dev = Start-WithLaunchId $launch {
            Start-Process -FilePath (Get-Command node -CommandType Application | Select-Object -First 1).Source `
                -ArgumentList @('node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', "$($script:DevPort)", '--strictPort') `
                -WorkingDirectory $script:FrontendDir -NoNewWindow -PassThru
        }
        $null = $dev.Handle   # keeps the exit code readable once it has ended
        Wait-Launch -Url "http://127.0.0.1:$($script:DevPort)/__vault/launch" -Launch $launch -Process $dev `
            -Name 'dev server' -Hint 'see the messages above' -Minutes 2
        $token = Read-Token $facts.vault
        $address = Get-AppAddress -HostName '127.0.0.1' -Port $script:DevPort -Token $token
        Open-App $address
        Write-Host "The dev server is at http://127.0.0.1:$($script:DevPort)/ . Press Ctrl+C to stop it and the server. The server does not reload: stop and start dev after a backend change."
        # A short wait in a loop, so that Ctrl+C reaches this script between the waits.
        while (-not $dev.WaitForExit(500)) { }
        if ($dev.ExitCode -ne 0) {
            throw "the dev server exited with code $($dev.ExitCode)"
        }
    }
    finally {
        if (($null -ne $dev) -and -not $dev.HasExited) {
            Stop-Process -Id $dev.Id -Force -ErrorAction SilentlyContinue
            $null = $dev.WaitForExit(10000)
        }
        # Under the lock, like every other change to the record.
        Invoke-Locked { Stop-Backend $server $serverStarted }
    }
}

function Main {
    if (-not $Command) {
        Write-Host 'Usage: .\vault.ps1 <install|run|stop|dev> [-NoBrowser]   (Get-Help .\vault.ps1 for details)'
        exit 2
    }
    try {
        switch ($Command) {
            'install' { Invoke-Install }
            'run' { Invoke-Run }
            'stop' { Invoke-Stop }
            'dev' { Invoke-Dev }
        }
    }
    catch {
        [Console]::Error.WriteLine("vault: $($_.Exception.Message)")
        exit 1
    }
}

# Dot-sourcing the file (the tests do) loads the functions without running a command.
if ($MyInvocation.InvocationName -ne '.') { Main }
