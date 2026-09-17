# Lance le tunnel Cloudflare « leradar » (appelé par la tâche planifiée
# « Candidature Radar - tunnel » à l'ouverture de session).
# La configuration du tunnel est dans %USERPROFILE%\.cloudflared\config.yml.
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$logs = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$log = Join-Path $logs "tunnel.log"
if ((Test-Path $log) -and (Get-Item $log).Length -gt 5MB) { Move-Item -Force $log "$log.old" }

$exe = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
if (-not (Test-Path $exe)) { $exe = "cloudflared" }
& $exe tunnel run leradar *>> $log
