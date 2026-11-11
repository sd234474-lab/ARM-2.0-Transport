[CmdletBinding()]
param([string[]]$ProtectedRoot = @())
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# READ ONLY: no service/task changes, no process launch/stop, no config values.
$report = [ordered]@{ObservedAt = (Get-Date -Format o); Errors = @()}
function Read-Fact([string]$Name, [scriptblock]$Read) {
    try { $report[$Name] = @(& $Read) }
    catch { $report[$Name] = 'UNKNOWN'; $report.Errors += $Name }
}
Read-Fact OS { Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,OSArchitecture,TotalVisibleMemorySize }
Read-Fact CPU { Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,VirtualizationFirmwareEnabled,SecondLevelAddressTranslationExtensions }
Read-Fact Disk { Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object DeviceID,Size,FreeSpace }
Read-Fact Processes { Get-CimInstance Win32_Process | Select-Object ProcessId,Name,ExecutablePath }
Read-Fact Ports { Get-NetTCPConnection -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess }
Read-Fact Services { Get-CimInstance Win32_Service | Select-Object Name,DisplayName,State,StartMode }
Read-Fact Tasks { Get-ScheduledTask | Select-Object TaskPath,TaskName,State }
Read-Fact Firewall { Get-NetFirewallRule | Select-Object DisplayName,Enabled,Direction,Action }
Read-Fact StartupNames {
    foreach ($key in @('HKCU:/Software/Microsoft/Windows/CurrentVersion/Run','HKLM:/Software/Microsoft/Windows/CurrentVersion/Run')) {
        if (Test-Path $key) { (Get-ItemProperty $key).PSObject.Properties.Name | Where-Object { $_ -notmatch '^PS' } }
    }
}
Read-Fact ProtectedRoots {
    foreach ($path in $ProtectedRoot) {
        [pscustomobject]@{Path=[IO.Path]::GetFullPath($path); Exists=(Test-Path -LiteralPath $path)}
    }
}
Read-Fact Shortcuts {
    $shell = New-Object -ComObject WScript.Shell
    foreach ($folder in @('Desktop','CommonDesktopDirectory','StartMenu','CommonStartMenu','Startup','CommonStartup')) {
        $root = [Environment]::GetFolderPath($folder)
        if ($root -and (Test-Path $root)) {
            Get-ChildItem -LiteralPath $root -Filter '*.lnk' -Recurse | ForEach-Object {
                $link = $shell.CreateShortcut($_.FullName)
                [pscustomobject]@{Shortcut=$_.FullName;Target=$link.TargetPath;WorkingDirectory=$link.WorkingDirectory}
            }
        }
    }
}
$docker = Get-Command docker -ErrorAction SilentlyContinue
$report.DockerCLI = if ($docker) {$docker.Source} else {'NOT FOUND; resources UNKNOWN'}
if ($docker) {
    $endpoint = & docker --context desktop-linux context inspect desktop-linux --format '{{json .Endpoints.docker.Host}}' 2>$null
    if ($LASTEXITCODE -or $endpoint -notmatch '^"npipe:.*dockerDesktopLinuxEngine"$') {
        $report.DockerResources = 'UNKNOWN: local Desktop Linux context not established; no remote engine queried'
        $report | ConvertTo-Json -Depth 7
        exit 0
    }
    # Queries only; never start Desktop/WSL or a stopped engine. Environment is not inspected.
    Read-Fact DockerProjects { & docker --context desktop-linux compose ls --all --format json; if ($LASTEXITCODE) {throw 'Docker query failed'} }
    Read-Fact DockerContainers { & docker --context desktop-linux ps -a --format '{{json .}}'; if ($LASTEXITCODE) {throw 'Docker query failed'} }
    Read-Fact DockerVolumes { & docker --context desktop-linux volume ls --format '{{json .}}'; if ($LASTEXITCODE) {throw 'Docker query failed'} }
    Read-Fact DockerNetworks { & docker --context desktop-linux network ls --format '{{json .}}'; if ($LASTEXITCODE) {throw 'Docker query failed'} }
    Read-Fact DockerMounts {
        foreach ($id in @(& docker --context desktop-linux ps -a --format '{{.ID}}')) {
            if ($id) { & docker --context desktop-linux inspect $id --format '{"name":{{json .Name}},"mounts":{{json .Mounts}},"networks":{{json .NetworkSettings.Networks}}}' }
        }
        if ($LASTEXITCODE) {throw 'Docker query failed'}
    }
}
$report | ConvertTo-Json -Depth 7
