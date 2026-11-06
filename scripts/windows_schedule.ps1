param(
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [string]$ConfigPath = "config\task.example.json",
    [string]$TaskName = "crawler4cert-local",
    [string]$At = "09:00",
    [switch]$Register
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = (Resolve-Path -LiteralPath $PythonPath).Path
$taskConfig = if ([System.IO.Path]::IsPathRooted($ConfigPath)) { $ConfigPath } else { Join-Path $projectRoot $ConfigPath }
$taskConfig = (Resolve-Path -LiteralPath $taskConfig).Path
$null = Get-Content -LiteralPath $taskConfig -Raw -Encoding UTF8 | ConvertFrom-Json
$time = [DateTime]::ParseExact($At, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)
$arguments = '-m autopapercrawl run --config "' + $taskConfig + '"'
$action = New-ScheduledTaskAction -Execute $pythonExecutable -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $time
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 6)
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings
if ($Register) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task | Select-Object TaskName, State
} else {
    Write-Output "Preview only. Add -Register to create the local task."
    Write-Output "Task: $TaskName; daily at $At"
    Write-Output "Working directory: $projectRoot"
    Write-Output "Executable: $pythonExecutable"
    Write-Output "Arguments: $arguments"
}
