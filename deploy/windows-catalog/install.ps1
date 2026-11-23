[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Stage)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$target = Join-Path ($env:SystemDrive + '\') 'ARM\00_START'
$desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'ARM - Catalog.lnk'
# This installer ADDS navigation only. No fallback, overwrite, deletion or app launch.
if (Test-Path -LiteralPath $target) {throw 'Refusing to overwrite existing 00_START'}
if (Test-Path -LiteralPath $desktopLink) {throw 'Refusing to overwrite existing shortcut'}
$stagePath = (Resolve-Path -LiteralPath $Stage).Path
$parent = Get-Item -LiteralPath (Split-Path $target -Parent)
for ($check = $parent; $null -ne $check; $check = $check.Parent) {
    if ($check.Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'Destination ancestor is a reparse point'}
}
$registry = Get-Content -LiteralPath (Join-Path $stagePath 'registry.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($registry.schema_version -ne 1 -or !$registry.all_prototypes) {throw 'Invalid local registry'}
$allowed = @('index.html','catalog.css','catalog.js','registry.json','READ_ME_FIRST.txt',
             '01_PLATFORMS','02_CONFIGURATIONS','03_INFOBASES','04_INSTANCES','05_RELEASES_BACKUPS','90_EVIDENCE')
foreach ($item in Get-ChildItem -LiteralPath $stagePath -Force) {
    if ($allowed -notcontains $item.Name -or $item.Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'Unexpected staged file/reparse point'}
}
foreach ($item in Get-ChildItem -LiteralPath $stagePath -Force -Recurse) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'Nested staged reparse point rejected'}
    if (!$item.PSIsContainer -and $item.Extension -in @('.exe','.cmd','.bat','.ps1','.lnk','.db','.sqlite','.sqlite3')) {throw 'Only catalogue documents/metadata may be copied'}
}
foreach ($required in $allowed) {if (!(Test-Path -LiteralPath (Join-Path $stagePath $required))) {throw 'Incomplete staged catalogue'}}
$categories = @('01_PLATFORMS','02_CONFIGURATIONS','03_INFOBASES','04_INSTANCES','05_RELEASES_BACKUPS')
if (@($registry.navigation).Count -eq 0) {throw 'A generic example is not an inspected local catalogue'}
$before = Get-Content -LiteralPath (Join-Path $stagePath '90_EVIDENCE/before.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if (@($before.protected_files).Count -eq 0) {throw 'Legacy protection evidence required'}
foreach ($protected in $before.protected_files) {
    $file = Get-Item -LiteralPath $protected.path -Force -ErrorAction Stop
    if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint -or $file.Length -ne $protected.bytes) {throw 'Legacy file changed/replaced; refresh read-only evidence'}
    if ((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash -ine $protected.sha256) {throw 'Legacy file fingerprint changed; no catalogue installed'}
}
foreach ($nav in $registry.navigation) {
    if ($categories -notcontains $nav.category -or $nav.id -notmatch '^[a-z][a-z0-9-]*$') {throw 'Unsafe navigation metadata'}
    # Navigation shortcuts may target ONLY directories, never executables/DB files.
    if (!(Test-Path -LiteralPath $nav.target -PathType Container)) {throw 'Navigation folder missing; refusing broken link'}
    if ($nav.target -notmatch '^[A-Za-z]:\\' -or $nav.target.Contains('..')) {throw 'Only explicit local folder paths supported'}
}
New-Item -ItemType Directory -Path $target -ErrorAction Stop | Out-Null
foreach ($item in Get-ChildItem -LiteralPath $stagePath -Force) {
    Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $target $item.Name) -Recurse -ErrorAction Stop
}
$shell = New-Object -ComObject WScript.Shell
foreach ($nav in $registry.navigation) {
    $linkPath = Join-Path (Join-Path $target $nav.category) ($nav.id + '.lnk')
    if (Test-Path -LiteralPath $linkPath) {throw 'Shortcut collision; existing link untouched'}
    $link = $shell.CreateShortcut($linkPath)
    $link.TargetPath = $nav.target
    $link.Description = 'Navigation only: opens a folder, not an ARM instance or database'
    $link.Save()
}
$link = $shell.CreateShortcut($desktopLink)
$link.TargetPath = Join-Path $target 'index.html'
$link.WorkingDirectory = $target
$link.Description = 'ARM prototype catalogue: platforms, configurations, infobases and launch instances'
$link.Save()
Write-Output ('Created navigation catalogue: ' + $target)
Write-Output ('New desktop entry: ' + $desktopLink)
Write-Output 'No existing installation, database, shortcut or launcher was changed. No ARM was launched.'
