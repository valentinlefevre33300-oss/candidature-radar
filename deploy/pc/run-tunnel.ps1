# Lance le tunnel Cloudflare « leradar » (appelé par la tâche planifiée
# « Candidature Radar - tunnel »).
# La configuration est passée explicitement : hors session interactive, le
# profil utilisateur (et donc %USERPROFILE%\.cloudflared) n'est pas garanti.
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$logs = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force $logs | Out-Null
$log = Join-Path $logs "tunnel.log"
if ((Test-Path $log) -and (Get-Item $log).Length -gt 5MB) { Move-Item -Force $log "$log.old" }

$config = "C:\Users\Valentin Lefevre\.cloudflared\config.yml"
$exe = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
if (-not (Test-Path $exe)) { $exe = "cloudflared" }
& $exe --config $config tunnel run leradar *>> $log
