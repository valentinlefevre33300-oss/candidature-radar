# Lance le serveur Candidature Radar sur le PC (appelé par la tâche planifiée
# « Candidature Radar - serveur » à l'ouverture de session).
# Écoute seulement en local : c'est le tunnel Cloudflare qui l'expose sur leradar.site.
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # deploy\pc -> racine du projet
Set-Location $root
$env:PYTHONUTF8 = "1"

$logs = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$log = Join-Path $logs "serveur.log"
if ((Test-Path $log) -and (Get-Item $log).Length -gt 5MB) { Move-Item -Force $log "$log.old" }

& "$root\.venv\Scripts\python.exe" -m uvicorn app.web.server:app --host 127.0.0.1 --port 8010 *>> $log
