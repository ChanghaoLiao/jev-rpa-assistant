$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot

$builder = Join-Path $projectRoot '.build-venv'
if (-not (Test-Path -LiteralPath (Join-Path $builder 'Scripts/python.exe'))) {
    $hostPython = $null
    foreach ($version in @('3.11', '3.13')) {
        $candidate = & py "-$version" -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $candidate) { $hostPython = $candidate; break }
    }
    if (-not $hostPython) { throw '打包需要 Python 3.11 或 3.13。请先安装官方 Python。' }
    & $hostPython -m venv $builder
    if ($LASTEXITCODE -ne 0) { throw '创建打包环境失败。' }
}
$builderPython = Join-Path $builder 'Scripts/python.exe'
& $builderPython -m pip install -r requirements-windows.txt 'pyinstaller>=6,<7'
if ($LASTEXITCODE -ne 0) { throw '安装打包依赖失败。' }
$env:JEVTOOL_DATA_DIR = Join-Path $projectRoot 'build/check-data'
$testTemp = Join-Path $projectRoot 'build/test-temp'
New-Item -ItemType Directory -Force -Path $testTemp | Out-Null
$env:TEMP = $testTemp
$env:TMP = $testTemp
$env:QT_QPA_PLATFORM = 'offscreen'
& $builderPython -m unittest discover -s tests -q
if ($LASTEXITCODE -ne 0) { throw '自动测试未通过，停止打包。' }
& $builderPython -m PyInstaller --noconfirm --clean --distpath release/audit-build --workpath build/audit-build packaging/JevReplyTool.spec
if ($LASTEXITCODE -ne 0) { throw '打包失败。' }

$release = Join-Path $projectRoot 'release'
New-Item -ItemType Directory -Force -Path $release | Out-Null
$archive = Join-Path $release 'JevReplyTool-Portable-0.3.5.zip'
if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive }
Compress-Archive -Path (Join-Path $projectRoot 'release/audit-build/JevReplyTool/*') -DestinationPath $archive

$isccCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
$isccPath = if ($isccCommand) { $isccCommand.Source } else { $null }
if (-not $isccPath) {
    $candidate = 'C:/Program Files (x86)/Inno Setup 6/ISCC.exe'
    if (Test-Path -LiteralPath $candidate) { $isccPath = $candidate }
}
if (-not $isccPath) {
    $candidate = Join-Path $projectRoot '.build-tools/InnoSetup/ISCC.exe'
    if (Test-Path -LiteralPath $candidate) { $isccPath = $candidate }
}
if ($isccPath) {
    & $isccPath 'packaging/JevReplyTool.iss'
    if ($LASTEXITCODE -ne 0) { throw '安装包生成失败。' }
} else {
    Write-Warning '没有找到 Inno Setup；已生成免安装便携包。安装器脚本已准备好。'
}
Write-Output "完成：$release"
