# Run the public installer with locally packaged release bytes on a Windows CI host.
param([Parameter(Mandatory)][string]$Cli)
$ErrorActionPreference = 'Stop'
$fixture = Join-Path $env:RUNNER_TEMP ('harnest-installer-' + [guid]::NewGuid())
$installer = Join-Path $PSScriptRoot '..\install.ps1'
[void][IO.Directory]::CreateDirectory($fixture)
try {
    $env:HARNEST_INSTALL_DIR = Join-Path $fixture 'bin with spaces'
    $env:HARNEST_RUNTIME_DIR = Join-Path $fixture 'runtime'
    $env:HARNEST_BOOTSTRAP_PYTHON = (Get-Command python.exe).Source
    $env:HARNEST_YES = '1'
    $env:HARNEST_VERSION = '1.2.3'
    $assetName = 'harnest_1.2.3_windows_' + $env:PROCESSOR_ARCHITECTURE.ToLowerInvariant() + '.zip'
    Copy-Item $Cli (Join-Path $fixture 'harnest.exe')
    $assetPath = Join-Path $fixture $assetName
    Compress-Archive (Join-Path $fixture 'harnest.exe') $assetPath
    $checksums = Join-Path $fixture 'checksums.txt'
    # Stub only the network boundary. Checksum verification, extraction, runtime
    # bootstrap and executable replacement all use the actual installer code.
    function Invoke-WebRequest($Uri, [switch]$UseBasicParsing, $Headers, $OutFile) {
        Copy-Item -LiteralPath (Join-Path $fixture (([uri]$Uri).Segments[-1])) -Destination $OutFile
    }
    Set-Content $checksums (('0' * 64) + '  ' + $assetName) -Encoding ascii
    try { & $installer; throw 'Installer accepted a bad checksum' }
    catch { if ($_.Exception.Message -notlike 'Checksum verification failed*') { throw } }
    if (Test-Path $env:HARNEST_RUNTIME_DIR) { throw 'Installer ran before checksum verification' }
    Set-Content $checksums ((Get-FileHash $assetPath).Hash + '  ' + $assetName) -Encoding ascii
    [void][IO.Directory]::CreateDirectory($env:HARNEST_INSTALL_DIR)
    $target = Join-Path $env:HARNEST_INSTALL_DIR 'harnest.exe'
    Set-Content $target 'previous installation'
    & $installer
    if ((Get-FileHash $target).Hash -ne (Get-FileHash $Cli).Hash) { throw 'Wrong installed executable' }
    & (Join-Path $env:HARNEST_RUNTIME_DIR 'Scripts\python.exe') -c 'import harnest.cli'
    if ($LASTEXITCODE -ne 0) { throw 'Installed runtime cannot import the CLI' }
} finally {
    [IO.Directory]::Delete($fixture, $true)
}
