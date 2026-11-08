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
$staged = $null
try {
    [void][IO.Directory]::CreateDirectory($tempDir)
    $manifest = Join-Path $tempDir 'checksums.txt'
    Invoke-WebRequest "$base/checksums.txt" -UseBasicParsing -Headers $headers -OutFile $manifest
    # Older pins and ARM64 releases may offer only the verified ZIP.
    $msiEntry = '^([0-9a-fA-F]{64})\s+\*?' + [regex]::Escape($asset) + '$'
    $useMsi = [bool](Select-String -LiteralPath $manifest -Pattern $msiEntry -CaseSensitive)
    if (!$useMsi) { $asset = "harnest_${version}_windows_${arch}.zip" }
    $download = Join-Path $tempDir $asset
    Invoke-WebRequest "$base/$asset" -UseBasicParsing -Headers $headers -OutFile $download
    $pattern = '^([0-9a-fA-F]{64})\s+\*?' + [regex]::Escape($asset) + '$'
    $entries = @(Select-String -LiteralPath $manifest -Pattern $pattern -CaseSensitive)
    if ($entries.Count -ne 1 -or (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash -ne $entries[0].Matches[0].Groups[1].Value) {
        throw "Checksum verification failed for $asset."
    }
    if (!$useMsi) {
        $installDir = if ($env:HARNEST_INSTALL_DIR) { $env:HARNEST_INSTALL_DIR } else { Join-Path $HOME '.local\bin' }
        $runtimeDir = if ($env:HARNEST_RUNTIME_DIR) { $env:HARNEST_RUNTIME_DIR } else { Join-Path $HOME '.harnest\runtime' }
        if ($env:HARNEST_YES -ne '1') {
            if ($env:HARNEST_YES) { throw 'HARNEST_YES must be 1 or unset.' }
            if ((Read-Host "Install Harnest $version to $installDir with runtime $runtimeDir? [y/N]") -notin 'y','yes') { throw 'Installation cancelled.' }
        }
        # Only extract the expected executable, regardless of other archive entries.
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $package = [IO.Compression.ZipFile]::OpenRead($download)
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
        return
    }
    $arguments = @('/i', ('"' + $download + '"'), '/norestart')
    if ($env:HARNEST_YES -eq '1') { $arguments += '/qn' }
    if ($env:HARNEST_INSTALL_DIR) {
        if ($env:HARNEST_INSTALL_DIR.Contains('"')) { throw 'HARNEST_INSTALL_DIR cannot contain a quote.' }
        $arguments += ('INSTALLFOLDER="' + $env:HARNEST_INSTALL_DIR + '"')
    }
    $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -notin 0,3010) { throw "Windows installation failed (exit $($process.ExitCode))." }
    Write-Host 'Installed Harnest. Open a new terminal and run harnest doctor.'
} finally {
    if ($staged -and [IO.File]::Exists($staged)) { [IO.File]::Delete($staged) }
    if ([IO.Directory]::Exists($tempDir)) { [IO.Directory]::Delete($tempDir, $true) }
}
