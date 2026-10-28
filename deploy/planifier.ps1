# Enregistre (ou met a jour) la tache planifiee de collecte.
#
#     .\deploy\planifier.ps1                 toutes les heures
#     .\deploy\planifier.ps1 -Minutes 30     toutes les 30 minutes
#     .\deploy\planifier.ps1 -Supprimer      retire la tache
#
# N'a pas besoin de droits administrateur : la tache est enregistree pour
# l'utilisateur courant et tourne sous son compte.

[CmdletBinding()]
param(
    [int]$Minutes = 60,
    [string]$Nom = 'Inventaire OVH-Proxmox',
    [switch]$Supprimer
)

$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'collecte.ps1'

if ($Supprimer) {
    Unregister-ScheduledTask -TaskName $Nom -Confirm:$false
    "tache supprimee : $Nom"
    return
}

if (-not (Test-Path $script)) { throw "script introuvable : $script" }

# pwsh (7+) si disponible, sinon le powershell.exe historique
$hote = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
if (-not $hote) { $hote = 'powershell.exe' }

$action = New-ScheduledTaskAction -Execute $hote `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""

# Repetition indefinie a partir de maintenant, plutot qu'a heure fixe :
# la collecte repart toute seule apres un redemarrage.
$declencheur = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $Minutes)

$reglages = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $Nom -Action $action -Trigger $declencheur `
    -Settings $reglages -Description 'Collecte de l inventaire OVH et Proxmox' `
    -Force | Out-Null

"tache enregistree : $Nom (toutes les $Minutes min, via $hote)"
"  lancer tout de suite : Start-ScheduledTask -TaskName '$Nom'"
"  etat                 : Get-ScheduledTaskInfo -TaskName '$Nom'"
"  journal              : Get-Content `"$env:USERPROFILE\.inventaire-infra.log`" -Tail 20"
