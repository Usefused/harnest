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
    $script:started = $false
    function Invoke-WebRequest($Uri, [switch]$UseBasicParsing, $Headers, $OutFile) {
        Copy-Item -LiteralPath (Join-Path $fixture (([uri]$Uri).Segments[-1])) -Destination $OutFile
    }
    # The real MSI lifecycle is covered by smoke_windows_installer.py on Windows.
    function Start-Process($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru) {
        if ($FilePath -ne 'msiexec.exe' -or $ArgumentList[0] -ne '/i') { throw 'Expected native MSI installation' }
        if ($ArgumentList -notcontains '/qn') { throw 'Non-interactive approval was lost' }
        $script:started = $true
        return [pscustomobject]@{ ExitCode = 0 }
    }
    Set-Content $checksums (('0' * 64) + '  ' + $assetName) -Encoding ascii
    try { & $installer; throw 'Installer accepted a bad checksum' }
    catch { if ($_.Exception.Message -notlike 'Checksum verification failed*') { throw } }
    if ($script:started) { throw 'Installer ran before checksum verification' }
    Set-Content $checksums ((Get-FileHash $assetPath).Hash + '  ' + $assetName) -Encoding ascii
    & $installer
    if (!$script:started) { throw 'Verified MSI was not started' }
} finally {
    [IO.Directory]::Delete($fixture, $true)
}
