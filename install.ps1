# Install the checksum-verified Windows CLI and its managed Python runtime.
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
$installDir = if ($env:HARNEST_INSTALL_DIR) { $env:HARNEST_INSTALL_DIR } else { Join-Path $HOME '.local\bin' }
$runtimeDir = if ($env:HARNEST_RUNTIME_DIR) { $env:HARNEST_RUNTIME_DIR } else { Join-Path $HOME '.harnest\runtime' }
if ($env:HARNEST_YES -ne '1') {
    if ($env:HARNEST_YES) { throw 'HARNEST_YES must be 1 or unset.' }
    if ((Read-Host "Install Harnest $version to $installDir with runtime $runtimeDir? [y/N]") -notin 'y','yes') { throw 'Installation cancelled.' }
}
$archive = "harnest_${version}_windows_${arch}.zip"
$base = "https://github.com/$repo/releases/download/$tag"
$tempDir = Join-Path ([IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
$staged = $null
try {
    [void][IO.Directory]::CreateDirectory($tempDir)
    $zip = Join-Path $tempDir $archive
    $manifest = Join-Path $tempDir 'checksums.txt'
    Invoke-WebRequest "$base/$archive" -UseBasicParsing -Headers $headers -OutFile $zip
    Invoke-WebRequest "$base/checksums.txt" -UseBasicParsing -Headers $headers -OutFile $manifest
    $pattern = '^([0-9a-fA-F]{64})\s+\*?' + [regex]::Escape($archive) + '$'
    $entries = @(Select-String -LiteralPath $manifest -Pattern $pattern -CaseSensitive)
    if ($entries.Count -ne 1 -or (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash -ne $entries[0].Matches[0].Groups[1].Value) {
        throw "Checksum verification failed for $archive."
    }
    # Only extract the expected executable, regardless of other archive entries.
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $package = [IO.Compression.ZipFile]::OpenRead($zip)
    $exe = Join-Path $tempDir 'harnest.exe'
    try {
        $entry = $package.GetEntry('harnest.exe')
        if (!$entry) { throw 'Release archive has no harnest.exe.' }
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $exe)
    } finally { $package.Dispose() }
    & $exe runtime install --directory $runtimeDir
    if ($LASTEXITCODE -ne 0) { throw "Runtime installation failed (exit $LASTEXITCODE)." }
    [void][IO.Directory]::CreateDirectory($installDir)
    $target = Join-Path $installDir 'harnest.exe'
    $staged = Join-Path $installDir ([guid]::NewGuid().ToString() + '.exe')
    [IO.File]::Copy($exe, $staged)
    if ([IO.File]::Exists($target)) {
        $backup = Join-Path $installDir ([guid]::NewGuid().ToString() + '.bak')
        [IO.File]::Replace($staged, $target, $backup)
        [IO.File]::Delete($backup)
    }
    else { [IO.File]::Move($staged, $target) }
    Write-Host "Installed Harnest $version at $target. Add $installDir to PATH and run harnest doctor."
} finally {
    if ($staged -and [IO.File]::Exists($staged)) { [IO.File]::Delete($staged) }
    if ([IO.Directory]::Exists($tempDir)) { [IO.Directory]::Delete($tempDir, $true) }
}
