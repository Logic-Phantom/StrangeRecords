# ============================================================
#  기묘한 기록 - Windows 작업 스케줄러 등록
#  매일 config.yaml 의 schedule.generate_time(기본 10:00)에 run_daily.bat 실행
#
#  실행: powershell -ExecutionPolicy Bypass -File scripts\register_windows_task.ps1
#  해제: Unregister-ScheduledTask -TaskName "StrangeRecords Daily Shorts" -Confirm:$false
# ============================================================
param(
    [string]$Time = "10:00",
    [string]$TaskName = "StrangeRecords Daily Shorts"
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BatPath = Join-Path $ProjectRoot "run_daily.bat"

if (-not (Test-Path $BatPath)) {
    Write-Error "run_daily.bat 을 찾을 수 없습니다: $BatPath"
    exit 1
}

$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BatPath`"" -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time
# StartWhenAvailable: 10:00 에 컴퓨터가 꺼져 있었다면 켜진 직후 실행
# WakeToRun: 절전 상태면 깨워서 실행
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "기묘한 기록: 매일 AI YouTube Shorts 제작 + 12:00 예약 공개" -Force | Out-Null

Write-Host "등록 완료: '$TaskName' 매일 $Time 실행 → $BatPath"
Write-Host "지금 바로 테스트: Start-ScheduledTask -TaskName `"$TaskName`""
