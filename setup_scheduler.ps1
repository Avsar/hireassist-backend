# setup_scheduler.ps1 -- Register daily HireAssist pipeline task
#
# Usage (run as Administrator):
#   powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1
#   powershell -ExecutionPolicy Bypass -File setup_scheduler.ps1 -Time "08:00"
#
# To remove:
#   Unregister-ScheduledTask -TaskName "HireAssist-DailyPipeline"

param(
    [string]$Time = "07:00",
    [string]$TaskName = "HireAssist-DailyPipeline"
)

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BatPath = Join-Path $ProjectDir "run_pipeline.bat"

if (-not (Test-Path $BatPath)) {
    Write-Error "run_pipeline.bat not found at $BatPath"
    exit 1
}

# Verify Python is available
$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    Write-Error "Python not found in PATH. Please install Python or add it to PATH."
    exit 1
}

# Create log directory
$LogDir = Join-Path $ProjectDir "data\logs"
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

# Create the scheduled task
$Action = New-ScheduledTaskAction -Execute $BatPath -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

# Remove existing task if present
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "HireAssist daily pipeline: discover, scrape, ATS sync, stats, export, push to Render"

Write-Host ""
Write-Host "Scheduled task '$TaskName' registered successfully."
Write-Host "  Schedule:   Daily at $Time"
Write-Host "  Script:     $BatPath"
Write-Host "  Python:     $PythonExe"
Write-Host "  Log:        $LogDir\pipeline.log"
Write-Host ""
Write-Host "To test manually:  $BatPath"
Write-Host "To view/modify:    taskschd.msc -> Task Scheduler Library -> $TaskName"
Write-Host "To remove:         Unregister-ScheduledTask -TaskName '$TaskName'"
