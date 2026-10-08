param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'runs\local\dashboard.json')
)

$ErrorActionPreference = 'Stop'

function ConvertTo-WindowsArgument([string]$Value) {
    # Start-Process joins ArgumentList itself; quote each native argv item first.
    # Double backslashes before a quote or the closing quote (Windows CRT rules).
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return '"' + $escaped + '"'
}

$launcherLog = Join-Path $PSScriptRoot 'runs\local\launcher-errors.log'
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
