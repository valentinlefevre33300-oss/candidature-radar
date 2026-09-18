# Arrête et supprime les deux tâches planifiées (serveur + tunnel).
foreach ($name in @("Candidature Radar - serveur", "Candidature Radar - tunnel")) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        "tâche « $name » supprimée"
    }
}
