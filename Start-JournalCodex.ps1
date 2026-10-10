[CmdletBinding()]
param(
    [string]$ProjectPath = (Join-Path $PSScriptRoot 'demo-project'),
    [ValidateSet('mxc', 'elevated')][string]$Sandbox = 'mxc',
    [switch]$Test
)
$ErrorActionPreference = 'Stop'
$installed = Get-Command codex -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$cliPath = if ($installed) { $installed.Source } else { $null }
if (-not $cliPath) {
    $binRoot = Join-Path $env:LOCALAPPDATA 'OpenAI\Codex\bin'
    $cliPath = Get-ChildItem -LiteralPath $binRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match '^[a-f0-9]{16}$' } |
        Sort-Object LastWriteTime -Descending |
        ForEach-Object { Join-Path $_.FullName 'codex.exe' } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
}
if (-not $cliPath) { throw 'Codex CLI was not found in PATH or the desktop installation.' }
$previousPath = $env:Path
try {
    $env:Path = (Split-Path -LiteralPath $cliPath) + ';' + $env:Path
    if ($Test) {
        $pythonPath = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
        if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Python 3.13 was not found.' }
        & $pythonPath (Join-Path $PSScriptRoot 'native_test.py') --windows-sandbox $Sandbox --exercise-stop
    } else {
        $resolvedProject = (Resolve-Path -LiteralPath $ProjectPath).Path
        & $cliPath -c "windows.sandbox=$Sandbox" --no-daemon --sandbox workspace-write --add-dir $PSScriptRoot -C $resolvedProject
    }
    $resultCode = $LASTEXITCODE
} finally {
    $env:Path = $previousPath
}
exit $resultCode
