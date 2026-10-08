param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot 'runs\lan\deployment.json')
)

$ErrorActionPreference = 'Stop'
trap {
    $_.Exception.Message | Add-Content -LiteralPath (Join-Path $PSScriptRoot 'runs\lan\launcher-errors.log')
    exit 1
}
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
Remove-Item Env:FLFLOW_SHARE_PASSWORD -ErrorAction SilentlyContinue
if ($config.anonymous_lan -ne $true) {
    $securePassword = (Get-Content -LiteralPath $config.password_file -Raw).Trim() | ConvertTo-SecureString
    $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    try {
        $env:FLFLOW_SHARE_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    }
}

try {
    Set-Location -LiteralPath $PSScriptRoot
    $arguments = @(
        '-u', '-B', ('"' + (Join-Path $PSScriptRoot 'share_server.py') + '"'),
        '--projects-config', ('"' + $config.projects_config + '"'),
        '--host', $config.host, '--port', $config.port,
        '--tls-cert', ('"' + $config.tls_cert + '"'),
        '--tls-key', ('"' + $config.tls_key + '"'),
        '--refresh-interval', $config.refresh_interval
    )
    if ($config.anonymous_lan -eq $true) {
        $arguments += '--anonymous-lan'
    } elseif ($config.username) {
        $arguments += @('--username', ('"' + $config.username + '"'))
    }
    $service = Start-Process -FilePath $config.python -ArgumentList $arguments `
        -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -Wait -PassThru `
        -RedirectStandardOutput $config.log_file -RedirectStandardError ($config.log_file + '.stderr')
    exit $service.ExitCode
} finally {
    Remove-Item Env:FLFLOW_SHARE_PASSWORD -ErrorAction SilentlyContinue
}
