param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version,

    [string]$Repository = 'matt5797/Seokdam-STT',

    [string]$PythonExecutable = 'python'
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$releaseDir = Join-Path $projectRoot 'release'
$packageDir = Join-Path $releaseDir 'package'
$generatedVersion = Join-Path $projectRoot 'proto\build_version.py'

try {
    Remove-Item -LiteralPath $packageDir -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path $releaseDir, $packageDir | Out-Null
    Set-Content -Path $generatedVersion -Encoding ASCII -Value "APP_VERSION = '$Version'"

    & $PythonExecutable -c 'import fastapi, google.genai, grpc, jinja2, sounddevice, uvicorn'
    if ($LASTEXITCODE -ne 0) {
        throw 'Application dependencies are missing. Run: python -m pip install -r requirements.txt'
    }
    & $PythonExecutable -c 'import PyInstaller'
    if ($LASTEXITCODE -ne 0) {
        throw 'PyInstaller is missing. Run: python -m pip install pyinstaller'
    }

    & $PythonExecutable -m PyInstaller --noconfirm --clean (Join-Path $projectRoot 'Seokdam-STT.spec')
    if ($LASTEXITCODE -ne 0) { throw 'Seokdam-STT build failed.' }
    & $PythonExecutable -m PyInstaller --noconfirm --clean (Join-Path $projectRoot 'Seokdam-Updater.spec')
    if ($LASTEXITCODE -ne 0) { throw 'Seokdam-Updater build failed.' }

    Copy-Item (Join-Path $projectRoot 'dist\Seokdam-STT.exe') $packageDir
    @{ version = $Version } |
        ConvertTo-Json |
        Set-Content -Path (Join-Path $packageDir 'app-version.json') -Encoding UTF8

    $archiveName = "Seokdam-STT-$Version.zip"
    $archivePath = Join-Path $releaseDir $archiveName
    Compress-Archive -Path (Join-Path $packageDir '*') -DestinationPath $archivePath -Force
    $sha256 = (Get-FileHash -Algorithm SHA256 $archivePath).Hash.ToLowerInvariant()

    $manifest = [ordered]@{
        version = $Version
        download_url = "https://github.com/$Repository/releases/download/v$Version/$archiveName"
        sha256 = $sha256
        executable = 'Seokdam-STT.exe'
        min_launcher_version = '1.0.0'
    }
    $manifest |
        ConvertTo-Json |
        Set-Content -Path (Join-Path $releaseDir 'version.json') -Encoding UTF8

    Copy-Item (Join-Path $projectRoot 'dist\Seokdam-Updater.exe') $releaseDir
    Write-Host "Release files created in $releaseDir"
}
finally {
    Remove-Item -LiteralPath $generatedVersion -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $packageDir -Recurse -Force -ErrorAction SilentlyContinue
}
