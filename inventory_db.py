#!/usr/bin/env python3
"""
Couche MariaDB de l'inventaire : ecrit un snapshot complet par collecte,
relit le dernier snapshot exploitable, garde les saisies humaines.

Facade : le code vit dans des modules par domaine, que ce fichier reexporte
pour que le reste du projet n'ait qu'un nom a connaitre.

    db_base       connexion, schema et migrations, erreurs, controle des
                  saisies, journal
    db_snapshots  instantanes du parc, menage
    db_saisies    ventes, elements, corrections, roles, rentabilite
    db_vues       vues de rentabilite enregistrees

Connexion : section "db" de infra.yml, ou variables d'environnement
INVENTORY_DB_HOST / _PORT / _USER / _PASSWORD / _NAME.
"""

from __future__ import annotations

from db_base import (  # noqa: F401
    DEVISES,
    DOSSIER_MIGRATIONS,
    DbError,
    DictCursor,
    ORIGINE_CHAINE,
    PERIODES,
    SaisieInterdite,
    VenteInvalide,
    _NOM_MIGRATION,
    _date_or_none,
    _date_saisie,
    _js,
    _json_ok,
    _num_or_none,
    _ouvre,
    _prix,
    _texte,
    _traduit,
    appliquer_migrations,
    chemin_schema,
    connect,
    db_settings,
    empreinte_journal,
    ensure_schema,
    instructions_sql,
    journaliser,
    lire_journal,
    migrations_disponibles,
    pymysql,
    tete_journal,
    verifier_journal,
    version_attendue,
    version_schema,
)
from db_snapshots import (  # noqa: F401
    load_latest,
    purge,
    purger_jetons_clients,
    store_run,
)
from db_saisies import (  # noqa: F401
    CHAMPS,
    CHAMPS_ELEMENT,
    CIBLES,
    CIBLES_ROLE,
    LOT_MAX,
    PARENTS,
    _CODE_ROLE,
    _REF_VM,
    _inserer_element,
    _role_existe,
    code_depuis_libelle,
    code_role,
    creer_role,
    desactiver_vente,
    enregistrer_element,
    enregistrer_elements,
    enregistrer_vente,
    lire_saisies,
    lister_ventes,
    poser_role,
    poser_surcharge,
    rentabilite,
    retirer_element,
    verifier_element,
    verifier_lot,
    verifier_ref_vm,
    verifier_role,
    verifier_role_carte,
    verifier_surcharge,
    verifier_vente,
)
from db_vues import (  # noqa: F401
    COLONNES_MAX,
    FIGURES_PANNEAU,
    GROUPES_PANNEAU,
    LISTES_VUE,
    MODES_VUE,
    NIVEAUX_DONNEES,
    NIVEAUX_VUE,
    PANNEAUX_MAX,
    TYPES_PANNEAU,
    _identifiant,
    _vue_lisible,
    _vue_modifiable,
    enregistrer_vue,
    lister_vues,
    retirer_vue,
    verifier_colonnes,
    verifier_panneau,
    verifier_vue,
)
