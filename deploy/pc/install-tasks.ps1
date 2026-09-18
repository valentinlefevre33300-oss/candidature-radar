# Enregistre (ou remplace) deux tâches planifiées : le serveur et le tunnel
# Cloudflare. Les actions lancent directement les programmes (pythonw sans
# console, cloudflared derrière conhost --headless), sans enveloppe PowerShell :
# les enveloppes PowerShell se faisaient tuer. Les tâches sont auto-réparatrices :
# un déclencheur se répète chaque minute ; si le processus vit, la nouvelle
# instance est ignorée (IgnoreNew), sinon il repart dans la minute.
#   .\install-tasks.ps1               (PowerShell administrateur) : démarrage au
#       boot de Windows, sans attendre l'ouverture de session (S4U : pas de mot
#       de passe à stocker), hors de la session interactive. Mode recommandé.
#   .\install-tasks.ps1 -Interactive  (sans droits admin) : lancées à l'ouverture
#       de session, dans la session.
# Journaux : data\logs\serveur.log et data\logs\tunnel.log.
# Désinstaller : .\deploy\pc\uninstall-tasks.ps1
param([switch]$Interactive)

$here = $PSScriptRoot
$root = Split-Path -Parent (Split-Path -Parent $here)
New-Item -ItemType Directory -Force (Join-Path $root "data\logs") | Out-Null

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

$cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$config = Join-Path $env:USERPROFILE ".cloudflared\config.yml"
$tasks = @(
    @{ Name = "Candidature Radar - serveur"
       Exe = "$root\.venv\Scripts\pythonw.exe"
       Args = "-X utf8 -m uvicorn app.web.server:app --host 127.0.0.1 --port 8010 --log-config `"$here\logging.json`""
       Dir = $root },
    @{ Name = "Candidature Radar - tunnel"
       Exe = "$env:SystemRoot\System32\conhost.exe"
       Args = "--headless `"$cloudflared`" --config `"$config`" --logfile `"$root\data\logs\tunnel.log`" tunnel run leradar"
       Dir = $root }
)
foreach ($t in $tasks) {
    $action = New-ScheduledTaskAction -Execute $t.Exe -Argument $t.Args -WorkingDirectory $t.Dir
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
