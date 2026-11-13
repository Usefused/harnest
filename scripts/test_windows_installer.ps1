# Verify the optional script downloads and verifies the MSI before invoking Windows Installer.
param([Parameter(Mandatory)][string]$Cli)
$ErrorActionPreference = 'Stop'
$fixture = Join-Path $env:RUNNER_TEMP ('harnest-installer-' + [guid]::NewGuid())
$installer = Join-Path $PSScriptRoot '..\install.ps1'
[void][IO.Directory]::CreateDirectory($fixture)
try {
    $env:HARNEST_YES = '1'
    $env:HARNEST_VERSION = '1.2.3'
    $assetName = 'harnest_1.2.3_windows_' + $env:PROCESSOR_ARCHITECTURE.ToLowerInvariant() + '.msi'
    $assetPath = Join-Path $fixture $assetName
    Copy-Item $Cli $assetPath
    $checksums = Join-Path $fixture 'checksums.txt'
    $probe = @{ Started = $false }
    function Invoke-WebRequest($Uri, [switch]$UseBasicParsing, $Headers, $OutFile) {
        Copy-Item -LiteralPath (Join-Path $fixture (([uri]$Uri).Segments[-1])) -Destination $OutFile
    }
    # The real MSI lifecycle is covered by smoke_windows_installer.py on Windows.
    function Start-Process($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru) {
        if ($FilePath -ne 'msiexec.exe' -or $ArgumentList[0] -ne '/i') { throw 'Expected native MSI installation' }
        if ($ArgumentList -notcontains '/qn') { throw 'Non-interactive approval was lost' }
        $probe.Started = $true
        return [pscustomobject]@{ ExitCode = 0 }
    }
    Set-Content $checksums (('0' * 64) + '  ' + $assetName) -Encoding ascii
    try { & $installer; throw 'Installer accepted a bad checksum' }
    catch { if ($_.Exception.Message -notlike 'Checksum verification failed*') { throw } }
    if ($probe.Started) { throw 'Installer ran before checksum verification' }
    Set-Content $checksums ((Get-FileHash $assetPath).Hash + '  ' + $assetName) -Encoding ascii
    & $installer
    if (!$probe.Started) { throw 'Verified MSI was not started' }
    # A pinned release without an MSI still uses the verified archive and native runtime bootstrap.
    $archiveName = [IO.Path]::ChangeExtension($assetName, '.zip')
    $archive = Join-Path $fixture $archiveName
    Copy-Item $Cli (Join-Path $fixture 'harnest.exe')
    Compress-Archive (Join-Path $fixture 'harnest.exe') $archive
    Set-Content $checksums ((Get-FileHash $archive).Hash + '  ' + $archiveName) -Encoding ascii
    $env:HARNEST_INSTALL_DIR = Join-Path $fixture 'bin with spaces'
    $env:HARNEST_RUNTIME_DIR = Join-Path $fixture 'runtime'
    $env:HARNEST_BOOTSTRAP_PYTHON = (Get-Command python.exe).Source
    $probe.Started = $false
    & $installer
    if ($probe.Started -or !(Test-Path (Join-Path $env:HARNEST_INSTALL_DIR 'harnest.exe'))) {
        throw 'ZIP-only release installation failed'
    }

} finally {
    [IO.Directory]::Delete($fixture, $true)
}
