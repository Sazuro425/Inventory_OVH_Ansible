# Collecte planifiee de l'inventaire (Windows).
#
# Enregistrement de la tache (PowerShell en administrateur) :
#     .\deploy\planifier.ps1
#
# Les secrets sont lus depuis un fichier d'environnement, jamais ecrits dans
# la definition de la tache planifiee : celle-ci est lisible par tous.

[CmdletBinding()]
param(
    [string]$EnvFile = "$env:USERPROFILE\.inventaire-infra.env",
    [string]$LogFile = "$env:USERPROFILE\.inventaire-infra.log"
)

$ErrorActionPreference = 'Stop'
$racine = Split-Path -Parent $PSScriptRoot

function Write-Log($message) {
    $ligne = "{0} {1}" -f (Get-Date -Format 's'), $message
    Write-Output $ligne
    Add-Content -Path $LogFile -Value $ligne -Encoding utf8
}

if (-not (Test-Path $EnvFile)) {
    Write-Log "ERREUR fichier d'environnement absent : $EnvFile"
    Write-Log "copie deploy\inventaire.env.example et renseigne-le"
    exit 78
}

# Format KEY=VALUE, les lignes vides et les commentaires sont ignores
Get-Content $EnvFile | ForEach-Object {
    $ligne = $_.Trim()
    if ($ligne -and -not $ligne.StartsWith('#')) {
        $i = $ligne.IndexOf('=')
        if ($i -gt 0) {
            $nom = $ligne.Substring(0, $i).Trim()
            $valeur = $ligne.Substring($i + 1).Trim()
            if ($valeur) { Set-Item -Path "env:$nom" -Value $valeur }
        }
    }
}

foreach ($v in @('OVH_APPLICATION_KEY', 'OVH_APPLICATION_SECRET',
                 'OVH_CONSUMER_KEY', 'PVE_TOKEN', 'INVENTORY_DB_PASSWORD')) {
    if (-not (Get-Item "env:$v" -ErrorAction SilentlyContinue).Value) {
        Write-Log "ERREUR variable manquante dans $EnvFile : $v"
        exit 78
    }
}

# Un seul passage a la fois : deux collectes concurrentes ecriraient deux
# snapshots en parallele.
$verrou = Join-Path $env:TEMP 'inventaire-infra.lock'
$flux = $null
try {
    $flux = [System.IO.File]::Open(
        $verrou, 'OpenOrCreate', 'ReadWrite', 'None')
} catch {
    Write-Log "une collecte est deja en cours, passage ignore"
    exit 0
}

try {
    Push-Location $racine
    $debut = Get-Date
    Write-Log "--- collecte ---"
    # Python journalise sa progression sur stderr. Avec ErrorActionPreference
    # a 'Stop', PowerShell en ferait une exception fatale des la premiere
    # ligne : on repasse en 'Continue' le temps de l'appel.
    $prec = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $exe = if ($env:INVENTAIRE_PYTHON) { $env:INVENTAIRE_PYTHON } else { 'python' }
        $sortie = & $exe infra_inventory.py --refresh --store-db 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prec
    }
    $sortie | Where-Object { $_ } | ForEach-Object { Write-Log "  $_" }
    if ($code -eq 0) {
        $duree = [int]((Get-Date) - $debut).TotalSeconds
        Write-Log "collecte terminee en ${duree}s"
    } else {
        Write-Log "ERREUR echec de la collecte (code $code)"
    }
    exit $code
} catch {
    Write-Log "ERREUR inattendue : $($_.Exception.Message)"
    exit 1
} finally {
    Pop-Location
    if ($flux) { $flux.Close() }
}
