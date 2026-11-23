[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidateSet('Inspect','Prepare','Validate','Build','Start','Health','CreateAdmin','Smoke','Stop','Cleanup')][string]$Action,
    [string]$GateFile,
    [switch]$OwnerApproved
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$project = 'arm2-uat-r2-001'
$package = 'ARM2-WIN-UAT-R2-001'
$source = '1f8c06d1f9269146ce7c07508b02dd09c89ff763'
$services = @('uat-db','uat-migrate','uat-api','uat-web')
$names = @('arm2-uat-r2-001-web','arm2-uat-r2-001-api','arm2-uat-r2-001-migrate','arm2-uat-r2-001-db')
$volumes = @('arm2-uat-r2-001-db','arm2-uat-r2-001-data')
$network = 'arm2-uat-r2-001-net'
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..')).TrimEnd('\')
$expectedRoot = [IO.Path]::Combine($env:SystemDrive + '\','ARM2-UAT',$package)
$local = Join-Path $repo '.local/arm2-uat-r2-001'
$envFile = Join-Path $local 'uat.env'

if ($Action -eq 'Inspect') { & (Join-Path $PSScriptRoot 'inventory.ps1'); exit 0 }
if ($repo -ine $expectedRoot) {throw 'NO-GO: use a separate clone in <SystemDrive>\ARM2-UAT\ARM2-WIN-UAT-R2-001'}
for ($item = Get-Item -LiteralPath $repo; $null -ne $item; $item = $item.Parent) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {throw 'NO-GO: reparse-point path'}
}
foreach ($key in @('DOCKER_HOST','DOCKER_CONTEXT','COMPOSE_FILE','COMPOSE_PROJECT_NAME','COMPOSE_PROFILES')) {
    if ([Environment]::GetEnvironmentVariable($key)) {throw "NO-GO: inherited $key must be cleared/reviewed manually"}
}
$dockerExe = (Get-Command docker.exe -CommandType Application -ErrorAction Stop).Source
function Docker([string[]]$Arguments) {
    $result = & $dockerExe --context desktop-linux @Arguments
    if ($LASTEXITCODE -ne 0) {throw 'Docker command failed; do not switch to root Compose'}
    return $result
}
function Compose([string[]]$Arguments) {
    Docker (@('compose','--project-name',$project,'--project-directory',$PSScriptRoot,'--env-file',$envFile,'--file',(Join-Path $PSScriptRoot 'compose.yaml')) + $Arguments)
}
function Require-OwnerGate {
    if (!$OwnerApproved -or !$GateFile) {throw 'NO-GO: explicit owner approval and completed gate evidence required'}
    $gate = Get-Content -LiteralPath $GateFile -Raw | ConvertFrom-Json
    foreach ($key in @('InventoryReviewed','NoOverlapConfirmed','BackupRestoreVerified','Arm1BaselinePassed')) {
        if ($gate.$key -ne $true) {throw "NO-GO: $key"}
    }
    foreach ($key in @('OwnerDecision','InventoryEvidence','BackupRestoreEvidence','Arm1BaselineEvidence')) {
        if ([string]::IsNullOrWhiteSpace($gate.$key)) {throw "NO-GO: missing $key"}
    }
    if (!$gate.ProtectedRoots -or $gate.ProtectedRoots.Count -eq 0) {throw 'NO-GO: list protected installation/data/config/backup roots in the gate'}
    foreach ($protected in $gate.ProtectedRoots) {
        $path = [IO.Path]::GetFullPath($protected).TrimEnd('\')
        if ($repo -ieq $path -or $repo.StartsWith($path+'\',[StringComparison]::OrdinalIgnoreCase) -or $path.StartsWith($repo+'\',[StringComparison]::OrdinalIgnoreCase)) {throw 'NO-GO: protected root overlaps UAT clone'}
    }
    $os = Get-CimInstance Win32_OperatingSystem
    $build = [int]($os.Version.Split('.')[-1])
    if ($build -lt 19045 -or ($build -ge 22000 -and $build -lt 22631) -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64' -or ![Environment]::Is64BitProcess) {throw 'NO-GO: supported x64 Windows/PowerShell required; do not upgrade this PC as part of UAT'}
    $wsl = & wsl.exe --version
    if ($LASTEXITCODE -or ($wsl -join ' ') -notmatch '(\d+\.\d+\.\d+)') {throw 'NO-GO: WSL version not established; do not install/update it here'}
    if ([version]$Matches[1] -lt [version]'2.1.5') {throw 'NO-GO: WSL 2.1.5+ required'}
    & git -C $repo merge-base --is-ancestor $source HEAD
    if ($LASTEXITCODE) {throw 'NO-GO: source SHA absent from package ancestry'}
    & git -C $repo diff --quiet $source -- backend frontend .dockerignore
    if ($LASTEXITCODE) {throw 'NO-GO: approved application tree changed'}
    $dirty = & git -C $repo status --porcelain --untracked-files=all -- backend frontend deploy/arm2-windows-uat .dockerignore
    if ($LASTEXITCODE -or $dirty) {throw 'NO-GO: source/package tree not clean'}
}
function Check-Docker {
    $endpoint = Docker @('context','inspect','desktop-linux','--format','{{json .Endpoints.docker.Host}}')
    if ($endpoint -notmatch '^"npipe:.*dockerDesktopLinuxEngine"$') {throw 'NO-GO: expected local Desktop Linux engine'}
    if ((Docker @('info','--format','{{.OSType}}')) -ne 'linux') {throw 'NO-GO: Linux containers required'}
}
function Check-Labels($labels) {
    if (!$labels -or $labels.'io.arm2.uat.package' -ne $package -or $labels.'io.arm2.uat.source' -ne $source) {throw 'NO-GO: foreign/mismatched resource; nothing stopped or removed'}
}
function Check-Ownership {
    $script:ownedContainers = @{}
    $existing = @(Docker @('ps','-a','--format','{{.Names}}'))
    foreach ($name in $names) {
        if ($existing -contains $name) {
            $info = (Docker @('inspect',$name) | ConvertFrom-Json)[0]
            Check-Labels $info.Config.Labels
            if ($info.Config.Labels.'com.docker.compose.project' -ne $project) {throw 'NO-GO: wrong Compose owner'}
            $script:ownedContainers[$name] = $info.Id
            foreach ($mount in $info.Mounts) {
                if ($mount.Type -eq 'bind' -or ($mount.Type -eq 'volume' -and $volumes -notcontains $mount.Name)) {throw 'NO-GO: unexpected container mount'}
            }
        }
    }
    $allVolumes = @(Docker @('volume','ls','--format','{{.Name}}'))
    foreach ($volume in $volumes) {
        if ($allVolumes -contains $volume) {
            $info = (Docker @('volume','inspect',$volume) | ConvertFrom-Json)[0]
            Check-Labels $info.Labels
            foreach ($id in @(Docker @('ps','-a','--filter',('volume='+$volume),'--format','{{.ID}}'))) {
                if ($id) {
                    $attached = (Docker @('inspect',$id) | ConvertFrom-Json)[0]
                    Check-Labels $attached.Config.Labels
                    if ($names -notcontains $attached.Name.TrimStart('/') -or $attached.Config.Labels.'com.docker.compose.project' -ne $project) {throw 'NO-GO: foreign attachment to UAT volume'}
                }
            }
        }
    }
    if (@(Docker @('network','ls','--format','{{.Name}}')) -contains $network) {
        $info = (Docker @('network','inspect',$network) | ConvertFrom-Json)[0]
        Check-Labels $info.Labels
        if (!$info.Internal) {throw 'NO-GO: external-capable network'}
        foreach ($property in $info.Containers.PSObject.Properties) {
            $attached = (Docker @('inspect',$property.Name) | ConvertFrom-Json)[0]
            Check-Labels $attached.Config.Labels
            if ($names -notcontains $attached.Name.TrimStart('/') -or $attached.Config.Labels.'com.docker.compose.project' -ne $project) {throw 'NO-GO: foreign attachment to UAT network'}
        }
    }
}
function Check-Env {
    if (!(Test-Path -LiteralPath $envFile)) {throw 'NO-GO: run Prepare after approval'}
    $lines = @(Get-Content -LiteralPath $envFile)
    if ($lines.Count -ne 2 -or $lines[0] -notmatch '^UAT_DB_PASSWORD=[a-f0-9]{64}$' -or $lines[1] -notmatch '^UAT_INERT_SECRET=[a-f0-9]{64}$') {throw 'NO-GO: invalid local UAT env; values suppressed'}
    # Validate without printing interpolated secrets.
    $config = Compose @('config','--format','json') | ConvertFrom-Json
    if ((@($config.services.PSObject.Properties.Name | Sort-Object) -join ',') -ne 'uat-api,uat-db,uat-migrate,uat-web') {throw 'NO-GO: unexpected services'}
    if (!$config.networks.'uat-net'.internal) {throw 'NO-GO: network must be internal'}
}
function Check-Port {
    $listening = @(Get-NetTCPConnection -State Listen -LocalPort 18082 -ErrorAction SilentlyContinue)
    if ($listening.Count) {
        $web = @(Docker @('ps','--filter','name=^/arm2-uat-r2-001-web$','--format','{{.Names}}'))
        if ($web -notcontains 'arm2-uat-r2-001-web') {throw 'NO-GO: proposed UAT port is occupied'}
    }
}
function Health {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:18082/api/health' -TimeoutSec 5
    if (!$health.ok -or $health.package -ne $package -or $health.source_sha -ne $source -or $health.migration -ne '0061' -or $health.database -ne 'arm2_uat_r2_001') {throw 'NO-GO: wrong endpoint/schema'}
    $health | Select-Object ok,package,source_sha,migration,database
}

if ($Action -eq 'Health') {Health; exit 0}
Check-Docker
Check-Ownership
if ($Action -in @('Stop','Cleanup')) {
    if (!$OwnerApproved) {throw 'Explicit owner approval required'}
    if ($Action -eq 'Cleanup' -and (Read-Host 'Type DELETE arm2-uat-r2-001 TEST DATA to erase ONLY UAT data') -cne 'DELETE arm2-uat-r2-001 TEST DATA') {throw 'Cancelled'}
    foreach ($name in $names) {
        if ($script:ownedContainers.ContainsKey($name)) {
            # Act on the inspected ID, never a replacement reusing its name.
            if ($Action -eq 'Stop') {Docker @('stop',$script:ownedContainers[$name])} else {Docker @('rm','--force',$script:ownedContainers[$name])}
        }
    }
    if ($Action -eq 'Cleanup') {
        foreach ($volume in $volumes) {if (@(Docker @('volume','ls','--format','{{.Name}}')) -contains $volume) {Docker @('volume','rm',$volume)}}
        if (@(Docker @('network','ls','--format','{{.Name}}')) -contains $network) {Docker @('network','rm',$network)}
    }
    Write-Output 'Only explicitly named UAT resources handled. Repeat ARM1 baseline now.'
    exit 0
}
Require-OwnerGate
Check-Port
if ($Action -eq 'Prepare') {
    if (Test-Path -LiteralPath $envFile) {throw 'Env already exists; do not overwrite credentials'}
    foreach ($volume in $volumes) {if (@(Docker @('volume','ls','--format','{{.Name}}')) -contains $volume) {throw 'NO-GO: old data exists without its env; recover original UAT config'}}
    $bytes = New-Object byte[] 32
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    $random.GetBytes($bytes); $password = ([BitConverter]::ToString($bytes)).Replace('-','').ToLowerInvariant()
    $random.GetBytes($bytes); $inert = ([BitConverter]::ToString($bytes)).Replace('-','').ToLowerInvariant(); $random.Dispose()
    New-Item -ItemType Directory -Path $local -Force | Out-Null
    [IO.File]::WriteAllText($envFile,"UAT_DB_PASSWORD=$password`nUAT_INERT_SECRET=$inert`n",[Text.UTF8Encoding]::new($false))
    $acl = Get-Acl -LiteralPath $envFile
    $acl.SetAccessRuleProtection($true,$false)
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.SetAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity,'FullControl','Allow'))
    Set-Acl -LiteralPath $envFile -AclObject $acl
    Write-Output 'Local env prepared; values suppressed. No containers started.'
    exit 0
}
Check-Env
switch ($Action) {
    'Validate' {Write-Output 'Static instance checks passed; this is NOT installation approval or ARM1 baseline acceptance.'}
    'Build' {Compose @('build','uat-api','uat-web')}
    'Start' {Compose @('up','--detach','--no-build','--wait','--wait-timeout','180','uat-db','uat-migrate','uat-api','uat-web'); Health}
    'CreateAdmin' {Health; Compose @('exec','uat-api','python','/uat/uat_control.py','create-admin')}
    'Smoke' {Health; Compose @('exec','uat-api','python','/uat/smoke.py')}
}
