# Optional compatibility entry point for the native Windows MSI installer.
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'This installer requires Windows.' }
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$repo = if ($env:HARNEST_REPO) { $env:HARNEST_REPO } else { 'Usefused/harnest' }
if ($repo -notmatch '^[\w.-]+/[\w.-]+$') { throw 'HARNEST_REPO must use owner/repository form.' }
$headers = @{}
if ($env:GITHUB_TOKEN) { $headers.Authorization = "Bearer $env:GITHUB_TOKEN" }
$tag = $env:HARNEST_VERSION
if (!$tag) { $tag = (Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers $headers).tag_name }
if (!$tag -or $tag -notmatch '^v?\d[\w.+-]*$') { throw 'Invalid Harnest release version.' }
$version = $tag -creplace '^v', ''
$tag = "v$version"
# Under emulation choose an archive for the native OS.
$machine = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
$arch = switch ($machine) { 'AMD64' { 'amd64' } 'ARM64' { 'arm64' } default { throw "Unsupported architecture: $machine" } }
# This optional compatibility entry point downloads the same native MSI offered
# in Releases. Windows Installer owns extraction, PATH, upgrades, and uninstall.
if ($env:HARNEST_YES -and $env:HARNEST_YES -ne '1') { throw 'HARNEST_YES must be 1 or unset.' }
$asset = "harnest_${version}_windows_${arch}.msi"
$base = "https://github.com/$repo/releases/download/$tag"
$tempDir = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
try {
    [void][IO.Directory]::CreateDirectory($tempDir)
    $msi = Join-Path $tempDir $asset
    $manifest = Join-Path $tempDir 'checksums.txt'
    Invoke-WebRequest "$base/$asset" -UseBasicParsing -Headers $headers -OutFile $msi
    Invoke-WebRequest "$base/checksums.txt" -UseBasicParsing -Headers $headers -OutFile $manifest
    $pattern = '^([0-9a-fA-F]{64})\s+\*?' + [regex]::Escape($asset) + '$'
    $entries = @(Select-String -LiteralPath $manifest -Pattern $pattern -CaseSensitive)
    if ($entries.Count -ne 1 -or (Get-FileHash -LiteralPath $msi -Algorithm SHA256).Hash -ne $entries[0].Matches[0].Groups[1].Value) {
        throw "Checksum verification failed for $asset."
    }
    $arguments = @('/i', ('"' + $msi + '"'), '/norestart')
    if ($env:HARNEST_YES -eq '1') { $arguments += '/qn' }
    if ($env:HARNEST_INSTALL_DIR) {
        if ($env:HARNEST_INSTALL_DIR.Contains('"')) { throw 'HARNEST_INSTALL_DIR cannot contain a quote.' }
        $arguments += ('INSTALLFOLDER="' + $env:HARNEST_INSTALL_DIR + '"')
    }
    $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -notin 0,3010) { throw "Windows installation failed (exit $($process.ExitCode))." }
    Write-Host 'Installed Harnest. Open a new terminal and run harnest doctor.'
} finally {
    if ([IO.Directory]::Exists($tempDir)) { [IO.Directory]::Delete($tempDir, $true) }
}
