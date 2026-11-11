[CmdletBinding()]
param([Parameter(Mandatory=$true)][string[]]$Root, [Parameter(Mandatory=$true)][string]$Output)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath $Output) {throw 'Refusing to overwrite evidence'}
$report = [ordered]@{ObservedAt=(Get-Date -Format o);Errors=@()}
function Matches-Root([string]$Value) {
    foreach ($path in $Root) {if ($Value -and $Value.IndexOf($path,[StringComparison]::OrdinalIgnoreCase) -ge 0) {return $true}}
    return $false
}
function Read-Fact([string]$Name,[scriptblock]$Read) {
    try {$report[$Name]=@(& $Read)} catch {$report[$Name]='NOT ESTABLISHED';$report.Errors+=$Name}
}
Read-Fact OS {Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,OSArchitecture}
Read-Fact Processes {
    Get-CimInstance Win32_Process | Where-Object {(Matches-Root $_.ExecutablePath) -or (Matches-Root $_.CommandLine)} |
        Select-Object ProcessId,Name,ExecutablePath
}
Read-Fact Ports {Get-NetTCPConnection -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess}
Read-Fact Services {
    Get-CimInstance Win32_Service | Where-Object {Matches-Root $_.PathName} | Select-Object Name,DisplayName,State,StartMode
}
Read-Fact Tasks {
    foreach ($task in Get-ScheduledTask) {
        foreach ($action in $task.Actions) {
            if (!$action.PSObject.Properties['Execute']) {continue}
            if ((Matches-Root $action.Execute) -or (Matches-Root $action.Arguments) -or (Matches-Root $action.WorkingDirectory)) {
                [pscustomobject]@{Name=$task.TaskName;Path=$task.TaskPath;State=$task.State;Executable=$action.Execute;WorkingDirectory=$action.WorkingDirectory}
            }
        }
    }
}
Read-Fact Shortcuts {
    $shell = New-Object -ComObject WScript.Shell
    foreach ($folder in @('Desktop','CommonDesktopDirectory','StartMenu','CommonStartMenu','Startup','CommonStartup')) {
        $directory = [Environment]::GetFolderPath($folder)
        if ($directory -and (Test-Path -LiteralPath $directory)) {
            foreach ($file in Get-ChildItem -LiteralPath $directory -Filter '*.lnk' -Recurse -ErrorAction SilentlyContinue) {
                $link=$shell.CreateShortcut($file.FullName)
                if ((Matches-Root $link.TargetPath) -or (Matches-Root $link.Arguments)) {
                    # Arguments are inspected only for path membership; never emitted.
                    [pscustomobject]@{Shortcut=$file.FullName;Target=$link.TargetPath;WorkingDirectory=$link.WorkingDirectory;Bytes=$file.Length;SHA256=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash}
                }
            }
        }
    }
}
Read-Fact StartupNames {
    foreach ($key in @('HKCU:/Software/Microsoft/Windows/CurrentVersion/Run','HKLM:/Software/Microsoft/Windows/CurrentVersion/Run')) {
        if (Test-Path $key) {
            $item=Get-ItemProperty $key
            foreach ($property in $item.PSObject.Properties) {
                if ($property.Name -notmatch '^PS' -and (Matches-Root ([string]$property.Value))) {$property.Name}
            }
        }
    }
}
Read-Fact FirewallNames {Get-NetFirewallRule | Where-Object {$_.DisplayName -match 'ARM|\u0410\u0420\u041c|Autotuning|Postgres'} | Select-Object DisplayName,Enabled,Direction,Action}
# Metadata only; never .Config.Env or connection strings.
$report.Docker = if (Get-Command docker.exe -ErrorAction SilentlyContinue) {'CLI found; engine/resources require separately reviewed local context'} else {'Not in PATH; projects/volumes NOT ESTABLISHED'}
$json=$report | ConvertTo-Json -Depth 6
$stream=[IO.File]::Open($Output,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write)
try {$writer=[IO.StreamWriter]::new($stream,[Text.UTF8Encoding]::new($false));$writer.Write($json);$writer.Flush()} finally {$stream.Dispose()}
Write-Output ('Read-only evidence written; unknown sections: ' + ($report.Errors -join ', '))
