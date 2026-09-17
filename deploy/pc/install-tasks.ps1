# Enregistre (ou remplace) deux tâches planifiées lancées à l'ouverture de
# session Windows : le serveur et le tunnel Cloudflare. Sans droits admin.
# Désinstaller : .\deploy\pc\uninstall-tasks.ps1
$here = $PSScriptRoot
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

$tasks = @(
    @{ Name = "Candidature Radar - serveur"; Script = "run-server.ps1" },
    @{ Name = "Candidature Radar - tunnel";  Script = "run-tunnel.ps1" }
)
foreach ($t in $tasks) {
    $args = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$here\$($t.Script)`""
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $args -WorkingDirectory $here
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName $t.Name
    "tâche « $($t.Name) » enregistrée et lancée"
}
