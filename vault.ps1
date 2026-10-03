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
    the browser's command line, a limit that ADR 0016 accepts.

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

# \A and \z, not ^ and $: in .NET (as in Python) $ also matches before a trailing newline.
$script:TokenShape = '\A[A-Za-z0-9_-]{32,128}\z'


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
print(json.dumps({
    "vault": None if settings.vault.path is None else str(settings.vault.path),
    "host": settings.server.host,
    "port": settings.server.port,
}))
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

function Write-Record {
    param([System.Diagnostics.Process]$Process)
    New-Item -ItemType Directory -Force -Path $script:RunDir | Out-Null
    $record = [ordered]@{ pid = $Process.Id; started = (Get-StartStamp $Process) }
    $record | ConvertTo-Json | Set-Content -LiteralPath $script:RecordFile -Encoding ASCII
}

function Remove-Record {
    Remove-Item -LiteralPath $script:RecordFile -Force -ErrorAction SilentlyContinue
}

function Get-RunningServer {
    <# The process the last "run" started, or $null. The record holds the process id and its start
       time, so a process that only reuses the number is never taken for the server. A record that
       cannot be read, or that names a process that has ended, is removed. #>
    if (-not (Test-Path -LiteralPath $script:RecordFile -PathType Leaf)) { return $null }
    try {
        $record = Get-Content -LiteralPath $script:RecordFile -Raw | ConvertFrom-Json
        $process = Get-Process -Id ([int]$record.pid) -ErrorAction Stop
        if ((Get-StartStamp $process) -eq [int64]$record.started) { return $process }
    }
    catch {
        # unreadable, or no such process: stale either way
    }
    Remove-Record
    return $null
}

function Start-Backend {
    <# Start the server hidden, remember it and wait until it answers. #>
    param([Parameter(Mandatory)][string]$HostName, [Parameter(Mandatory)][int]$Port)
    $python = Get-BackendPython
    New-Item -ItemType Directory -Force -Path $script:RunDir | Out-Null
    $process = Start-Process -FilePath $python -ArgumentList @('-m', 'vault') `
        -WorkingDirectory $script:BackendDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $script:RunDir 'server.out.log') `
        -RedirectStandardError (Join-Path $script:RunDir 'server.err.log')
    Write-Record $process
    $url = "http://${HostName}:${Port}/api/health"
    if ($HostName.Contains(':')) { $url = "http://[${HostName}]:${Port}/api/health" }
    $deadline = (Get-Date).AddSeconds(60)
    while ($true) {
        if ($process.HasExited) {
            Remove-Record
            throw 'the server stopped while starting; see .run\server.err.log'
        }
        try {
            $answer = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2
            if ($answer.StatusCode -eq 200) { return $process }
        }
        catch {
            # not listening yet
        }
        if ((Get-Date) -gt $deadline) {
            Stop-Backend $process
            throw 'the server did not answer within 60 seconds; see .run\server.err.log'
        }
        Start-Sleep -Milliseconds 500
    }
}

function Stop-Backend {
    param([System.Diagnostics.Process]$Process)
    if (-not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        $null = $Process.WaitForExit(10000)
    }
    Remove-Record
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

function Invoke-Run {
    $facts = Get-VaultFacts
    $running = Get-RunningServer
    if ($running) {
        Write-Host 'The server is already running.'
    }
    else {
        $null = Start-Backend -HostName $facts.host -Port $facts.port
    }
    $token = Read-Token $facts.vault
    $address = Get-AppAddress -HostName $facts.host -Port $facts.port -Token $token
    Open-App $address
    Write-Host "The app is at http://$($facts.host):$($facts.port)/ (the browser is opened with its token). Stop it with .\vault.ps1 stop"
}

function Invoke-Stop {
    $running = Get-RunningServer
    if (-not $running) {
        Write-Host 'The server is not running.'
        return
    }
    Stop-Backend $running
    Write-Host 'The server is stopped.'
}

function Invoke-Dev {
    Assert-Tool 'npm' 'Install Node 22.13 or newer: winget install OpenJS.NodeJS.LTS'
    if (-not (Test-Path -LiteralPath (Join-Path $script:FrontendDir 'node_modules') -PathType Container)) {
        throw 'the frontend is not installed yet; run .\vault.ps1 install first'
    }
    $facts = Get-VaultFacts
    if ($facts.port -ne 8765) {
        # frontend/vite.config.ts forwards /api to port 8765.
        throw "dev expects server.port 8765 (the Vite proxy is fixed to it), but the settings say $($facts.port)"
    }
    if (Get-RunningServer) {
        throw 'a server started by "run" is already running; stop it first with .\vault.ps1 stop'
    }
    $server = Start-Backend -HostName $facts.host -Port $facts.port
    try {
        $token = Read-Token $facts.vault
        $address = Get-AppAddress -HostName '127.0.0.1' -Port $script:DevPort -Token $token
        Open-App $address
        Write-Host "The dev server is at http://127.0.0.1:$($script:DevPort)/ . Press Ctrl+C to stop it and the server. The server does not reload: stop and start dev after a backend change."
        Invoke-Native 'npm' @('run', 'dev', '--', '--host', '127.0.0.1', '--port', "$($script:DevPort)", '--strictPort') $script:FrontendDir
    }
    finally {
        Stop-Backend $server
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
