$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BuildRoot = Join-Path $ProjectRoot "build\installer"
$PayloadRoot = Join-Path $BuildRoot "payload"
$DistRoot = Join-Path $ProjectRoot "dist"
$RuntimeArchive = Join-Path $BuildRoot "python-embed.zip"
$RuntimeRoot = Join-Path $BuildRoot "runtime"
$PythonUrl = "https://www.python.org/ftp/python/3.13.13/python-3.13.13-embed-amd64.zip"
$PythonSha256 = "8766a8775746235e23cf5aee5027ab1060bb981d93110577adcf3508aa0cbd55"
New-Item -ItemType Directory -Force $BuildRoot, $DistRoot | Out-Null
if (Test-Path $PayloadRoot) { Remove-Item -LiteralPath $PayloadRoot -Recurse -Force }
New-Item -ItemType Directory -Force $PayloadRoot | Out-Null
if ((-not (Test-Path $RuntimeArchive)) -or ((Get-FileHash $RuntimeArchive -Algorithm SHA256).Hash.ToLower() -ne $PythonSha256)) {
    Invoke-WebRequest -Uri $PythonUrl -OutFile $RuntimeArchive
}
if ((Get-FileHash $RuntimeArchive -Algorithm SHA256).Hash.ToLower() -ne $PythonSha256) { throw "Python runtime SHA-256 verification failed." }
if (Test-Path $RuntimeRoot) { Remove-Item -LiteralPath $RuntimeRoot -Recurse -Force }
New-Item -ItemType Directory -Force $RuntimeRoot | Out-Null
Expand-Archive -LiteralPath $RuntimeArchive -DestinationPath $RuntimeRoot
$PthFile = Get-ChildItem $RuntimeRoot -Filter "python*._pth" | Select-Object -First 1
@("python313.zip", ".", "Lib\site-packages", "..\src", "import site") | Set-Content $PthFile.FullName -Encoding ascii
$SitePackages = Join-Path $RuntimeRoot "Lib\site-packages"
New-Item -ItemType Directory -Force $SitePackages | Out-Null
& (Join-Path $ProjectRoot ".venv\Scripts\python.exe") -m pip install --disable-pip-version-check --target $SitePackages -r (Join-Path $ProjectRoot "requirements.txt") pip
if ($LASTEXITCODE -ne 0) { throw "Failed to prepare the private runtime dependencies." }
Copy-Item (Join-Path $ProjectRoot "src") $PayloadRoot -Recurse -Force
Copy-Item (Join-Path $ProjectRoot "requirements.txt") $PayloadRoot -Force
Copy-Item (Join-Path $PSScriptRoot "launch.cmd") $PayloadRoot -Force
$Compiler = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
if (-not (Test-Path $Compiler)) { $Compiler = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $Compiler)) { throw "Inno Setup 6 compiler was not found." }
Get-ChildItem -LiteralPath $DistRoot -File | Remove-Item -Force
& $Compiler "/DProjectRoot=$ProjectRoot" "/DRuntimeRoot=$RuntimeRoot" (Join-Path $PSScriptRoot "setup.iss")
$Target = Join-Path $DistRoot "Installer.exe"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $Target)) { throw "Inno Setup failed to create the installer." }
Get-Item $Target | Select-Object FullName, Length, LastWriteTime
