param(
    [string]$ConfigPath = (Join-Path (Split-Path $PSScriptRoot -Parent) 'runs\local\dashboard.json')
)

$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path $PSScriptRoot -Parent

function ConvertTo-WindowsArgument([string]$Value) {
    # Start-Process joins ArgumentList itself; quote each native argv item first.
    # Double backslashes before a quote or the closing quote (Windows CRT rules).
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

function Assert-DashboardPortAvailable([string[]]$Arguments, [int]$StartIndex) {
    $dashboardHost = '127.0.0.1'
    $dashboardPortText = '8765'
    for ($index = $StartIndex; $index -lt $Arguments.Count; $index++) {
        $argument = $Arguments[$index]
        $option = ($argument -split '=', 2)[0]
        # argparse permits these unambiguous abbreviations. Repeated options
        # use their last value, as the original flflow serve parser does.
        if ($option -notin @('--host', '--hos', '--ho', '--port', '--por', '--po')) { continue }
        if ($argument.Contains('=')) {
            $value = $argument.Substring($argument.IndexOf('=') + 1)
        } else {
            $index++
            if ($index -ge $Arguments.Count) { throw "Missing value for $option" }
            $value = $Arguments[$index]
        }
        if ($option -in @('--host', '--hos', '--ho')) { $dashboardHost = $value }
        else { $dashboardPortText = $value }
    }
    $dashboardPort = 0
    # Preserve port 0: the original Python server supports a random free port.
    if (-not [int]::TryParse($dashboardPortText, [ref]$dashboardPort) -or $dashboardPort -lt 0 -or $dashboardPort -gt 65535) {
        throw 'Dashboard port must be an integer from 0 to 65535'
    }
    $dashboardAddress = $null
    if ($dashboardHost -eq '') {
        $dashboardAddress = [Net.IPAddress]::Any
    } elseif (-not [Net.IPAddress]::TryParse($dashboardHost, [ref]$dashboardAddress)) {
        $addresses = @([Net.Dns]::GetHostAddresses($dashboardHost) | Where-Object { $_.AddressFamily -eq [Net.Sockets.AddressFamily]::InterNetwork })
        if ($addresses.Count -eq 0) { throw 'Dashboard host must resolve to an IPv4 address for flflow serve' }
        $dashboardAddress = $addresses[0]
    }
    if ($dashboardAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        throw 'Dashboard host must use IPv4 for flflow serve'
    }
    $portProbe = $null
    try {
        $portProbe = [Net.Sockets.TcpListener]::new($dashboardAddress, $dashboardPort)
        $portProbe.ExclusiveAddressUse = $true
        $portProbe.Start()
    } catch {
        throw "Cannot bind dashboard endpoint ${dashboardHost}:$dashboardPort; choose an unused port or check the local address. Existing services were not changed."
    } finally {
        if ($null -ne $portProbe) { $portProbe.Stop() }
    }
}

$launcherLog = Join-Path $AppRoot 'runs\local\launcher-errors.log'
try {
    $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($name in @('python', 'working_directory')) {
        if (-not ($config.$name -is [string]) -or -not $config.$name) {
            throw "Missing local launcher setting: $name"
        }
    }
    $python = (Resolve-Path -LiteralPath $config.python).ProviderPath
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or [IO.Path]::GetExtension($python) -ne '.exe') {
        throw 'python must name a native Python executable, not a shell shim'
    }
    $workingDirectory = (Resolve-Path -LiteralPath $config.working_directory).ProviderPath
    if (-not (Test-Path -LiteralPath $workingDirectory -PathType Container)) { throw 'Invalid working_directory' }
    if (-not ($config.arguments -is [array]) -or $config.arguments.Count -eq 0) { throw 'arguments must be an argv array' }
    foreach ($argument in $config.arguments) {
        if (-not ($argument -is [string]) -or $argument.Contains([char]0) -or $argument.Contains("`r") -or $argument.Contains("`n")) {
            throw 'Every argv item must be a string without control line breaks'
        }
    }
    $argv = @($config.arguments)
    if ($config.program) { $argv = @('-u', '-B', [string]$config.program) + $argv }
    $programIndex = 0
    while ($programIndex -lt $argv.Count -and @('-u', '-B') -contains $argv[$programIndex]) { $programIndex++ }
    if ($programIndex + 1 -ge $argv.Count -or [IO.Path]::GetFileName($argv[$programIndex]) -ne 'flflow.py' -or $argv[$programIndex + 1] -ne 'serve') {
        throw 'Only flflow.py serve is supported by this local dashboard launcher'
    }
    $program = $argv[$programIndex]
    if (-not [IO.Path]::IsPathRooted($program)) { $program = Join-Path $workingDirectory $program }
    $argv[$programIndex] = (Resolve-Path -LiteralPath $program).ProviderPath
    $logFile = if ($config.log_file) { [string]$config.log_file } else { [string]$config.logfile }
    if (-not $logFile) { throw 'Missing local launcher setting: log_file' }
    if (-not [IO.Path]::IsPathRooted($logFile)) { $logFile = Join-Path $workingDirectory $logFile }
    $logFile = [IO.Path]::GetFullPath($logFile)
    if ($logFile -eq $argv[$programIndex] -or $logFile -eq $python -or $logFile -eq [IO.Path]::GetFullPath($ConfigPath)) { throw 'Log path must not overwrite program or configuration' }
    # Check before creating/truncating service logs or starting another owner.
    Assert-DashboardPortAvailable -Arguments $argv -StartIndex ($programIndex + 2)
    New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($logFile)) -Force | Out-Null
    $launcherLog = $logFile + '.launcher.log'
    $argumentLine = ($argv | ForEach-Object { ConvertTo-WindowsArgument $_ }) -join ' '
    if ($argumentLine.Length + $python.Length -gt 30000) { throw 'Local dashboard command line is too long' }
    $service = Start-Process -FilePath $python -ArgumentList $argumentLine `
        -WorkingDirectory $workingDirectory -WindowStyle Hidden -Wait -PassThru `
        -RedirectStandardOutput $logFile -RedirectStandardError ($logFile + '.stderr')
    $service.Refresh()
    Add-Content -LiteralPath $launcherLog -Value ("{0:o} dashboard exited: {1}" -f (Get-Date), $service.ExitCode)
    exit $service.ExitCode
} catch {
    try {
        New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($launcherLog)) -Force | Out-Null
        Add-Content -LiteralPath $launcherLog -Value ("{0:o} {1}" -f (Get-Date), $_.Exception.Message)
    } catch { }
    [Console]::Error.WriteLine('Local dashboard launcher failed; see the local launcher log.')
    exit 1
}
