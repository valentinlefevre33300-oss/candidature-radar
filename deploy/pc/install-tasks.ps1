# Enregistre (ou remplace) deux tâches planifiées : le serveur et le tunnel
# Cloudflare. Elles sont auto-réparatrices : un déclencheur se répète toutes
# les minutes ; si le processus tourne encore, la nouvelle instance est ignorée
# (IgnoreNew), sinon il repart dans la minute.
#   .\install-tasks.ps1               (PowerShell administrateur) : démarrage au
#       boot de Windows, sans attendre l'ouverture de session (S4U : pas de mot
#       de passe à stocker), hors de la session interactive. Mode recommandé.
#   .\install-tasks.ps1 -Interactive  (sans droits admin) : lancées à l'ouverture
#       de session, dans la session.
# Désinstaller : .\deploy\pc\uninstall-tasks.ps1
param([switch]$Interactive)

$here = $PSScriptRoot
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$user = "$env:USERDOMAIN\$env:USERNAME"
$heartbeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
if ($Interactive) {
    $triggers = @((New-ScheduledTaskTrigger -AtLogOn -User $user), $heartbeat)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $mode = "a l'ouverture de session + relance chaque minute"
} else {
    $triggers = @((New-ScheduledTaskTrigger -AtStartup), $heartbeat)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
    $mode = "au demarrage, hors session + relance chaque minute"
}

$tasks = @(
    @{ Name = "Candidature Radar - serveur"; Script = "run-server.ps1" },
    @{ Name = "Candidature Radar - tunnel";  Script = "run-tunnel.ps1" }
)
foreach ($t in $tasks) {
    $args = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$here\$($t.Script)`""
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $args -WorkingDirectory $here
    try {
        Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $triggers -Settings $settings `
            -Principal $principal -Force -ErrorAction Stop | Out-Null
    } catch {
        Write-Error "impossible d'enregistrer « $($t.Name) » : $($_.Exception.Message) — sans droits admin, utiliser -Interactive"
        exit 1
    }
    Start-ScheduledTask -TaskName $t.Name
    "tache « $($t.Name) » enregistree et lancee ($mode)"
}
