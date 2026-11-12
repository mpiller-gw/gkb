"""Wer darf was sehen? Nutzer -> Principals -> sichtbare Rechte-Partitionen.

Regeln:
- Eine Partition ist sichtbar, wenn mindestens ein Principal des Nutzers erlaubt und keiner verboten ist.
- Unvollständige Partitionen (Rechte nicht lesbar) und Pfad-Fallback-Partitionen sehen nur Administratoren.
- Administratoren (GKB_ADMINS) sehen alles.
Der Filter wird VOR jeder Suche angewendet, nie nachträglich.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Nutzer:
    id: str
    principals: frozenset[str]
    admin: bool = False


def gruppen_aus_nextcloud(base_url: str, admin_user: str, admin_password: str, nutzer: str) -> list[str]:
    """Gruppen eines Nutzers über die Nextcloud-Provisioning-API (benötigt Admin-Zugang)."""
    if not (base_url and admin_user and admin_password):
        return []
    r = httpx.get(f"{base_url.rstrip('/')}/ocs/v2.php/cloud/users/{nutzer}/groups",
                  params={"format": "json"}, auth=(admin_user, admin_password),
                  headers={"OCS-APIRequest": "true"}, timeout=30)
    if r.status_code != 200:
        log.warning("Gruppen für %s nicht lesbar (HTTP %s)", nutzer, r.status_code)
        return []
    return list(r.json()["ocs"]["data"].get("groups", []))


def nutzer_laden(nutzer_id: str, settings) -> Nutzer:
    admins = {a.strip() for a in settings.admins.split(",") if a.strip()}
    gruppen = gruppen_aus_nextcloud(settings.nextcloud_url, settings.nextcloud_admin_user,
                                    settings.nextcloud_admin_password, nutzer_id)
    principals = frozenset({f"user:{nutzer_id}", *(f"group:{g}" for g in gruppen)})
    return Nutzer(nutzer_id, principals, nutzer_id in admins)


def sichtbare_partitionen(conn, nutzer: Nutzer) -> list[str]:
    if nutzer.admin:
        return [r["id"] for r in conn.execute("SELECT id FROM rechte_partition").fetchall()]
    rows = conn.execute(
        """SELECT id FROM rechte_partition
           WHERE vollstaendig
             AND erlaubt && %(p)s::text[]
             AND NOT (verboten && %(p)s::text[])
             AND NOT EXISTS (SELECT 1 FROM unnest(erlaubt) e WHERE e LIKE 'pfad:%%')""",
        {"p": sorted(nutzer.principals)},
    ).fetchall()
    return [r["id"] for r in rows]
