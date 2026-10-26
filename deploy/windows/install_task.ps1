# Регистрация ежедневного задания в Планировщике задач Windows (07:00).
# Запустите PowerShell от имени администратора из корня проекта:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1
#
# Параметры: -Time "07:00" -TaskName "MarketplaceParser".

param(
    [string]$TaskName = "MarketplaceParser",
    [string]$Time = "07:00"
)

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$batPath = Join-Path $projectRoot "deploy\windows\run_cycle.bat"

if (-not (Test-Path $batPath)) {
    throw "Не найден $batPath"
}

$action = New-ScheduledTaskAction -Execute $batPath -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Ежедневный парсинг маркетплейсов в $Time" `
    -Force | Out-Null

Write-Host "✅ Задание '$TaskName' создано: каждый день в $Time"
Write-Host "   Проверить: Get-ScheduledTask -TaskName $TaskName"
Write-Host "   Запустить сейчас: Start-ScheduledTask -TaskName $TaskName"
