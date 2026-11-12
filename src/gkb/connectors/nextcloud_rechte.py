"""Baustein 1b: Wer darf eine Datei in der Nextcloud lesen?

Ergebnis je Datei ist eine **Rechte-Partition**: eine Menge erlaubter Principals
(`user:<id>`, `group:<id>`) und eine Menge ausdrücklich verbotener Principals.
Dateien mit identischen Rechten teilen sich eine Partition.

Quellen der Rechte:
1. Freigaben (OCS Share API) an Nutzer und Gruppen, inklusive der Freigaben auf übergeordneten
   Ordnern (vererbt). Linkfreigaben, E-Mail- und föderierte Freigaben geben in GKB KEINE Rechte.
2. Eigentümer der freigegebenen Inhalte.
3. Gruppenordner (App "groupfolders"): Gruppen des Ordners (Admin-API, optional) und
   ACL-Regeln je Pfad (WebDAV-Property nc:acl-list), vererbt von oben nach unten.

Kann eine Quelle nicht gelesen werden, wird die Partition als `vollstaendig = false` markiert.
Die Abfrageschicht zeigt solche Inhalte nur Administratoren (sicherer Ausfall).
"""
from __future__ import annotations

import hashlib
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import quote

import httpx

from gkb.connectors.nextcloud import NS

log = logging.getLogger(__name__)

SHARE_USER, SHARE_GROUP = 0, 1
LESEN = 1  # Nextcloud-Berechtigungsbit "read"

ACL_PROPFIND = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns">
  <d:prop><nc:acl-list/><nc:acl-enabled/><nc:group-folder-id/></d:prop>
</d:propfind>"""


@dataclass(frozen=True)
class Partition:
    erlaubt: frozenset[str]
    verboten: frozenset[str] = frozenset()
    vollstaendig: bool = True
    hinweis: str = ""

    @property
    def id(self) -> str:
        key = "|".join(sorted(self.erlaubt)) + "#" + "|".join(sorted(self.verboten)) + f"#{self.vollstaendig}"
        return "rp-" + hashlib.sha256(key.encode()).hexdigest()[:12]


@dataclass
class AclRegel:
    typ: str        # user | group
    principal: str
    maske: int
    rechte: int

    @property
    def key(self) -> str:
        return f"{self.typ}:{self.principal}"


def parse_acl_list(xml_text: str) -> dict[str, list[AclRegel]]:
    """PROPFIND-Antwort -> {pfad-href: [Regeln]}"""
    root = ET.fromstring(xml_text)
    out: dict[str, list[AclRegel]] = {}
    for resp in root.findall("d:response", NS):
        href = resp.findtext("d:href", default="", namespaces=NS)
        regeln: list[AclRegel] = []
        for acl in resp.iter(f"{{{NS['nc']}}}acl"):
            regeln.append(AclRegel(
                typ=acl.findtext("nc:acl-mapping-type", default="", namespaces=NS),
                principal=acl.findtext("nc:acl-mapping-id", default="", namespaces=NS),
                maske=int(acl.findtext("nc:acl-mask", default="0", namespaces=NS)),
                rechte=int(acl.findtext("nc:acl-permissions", default="0", namespaces=NS)),
            ))
        out[href] = regeln
    return out


def wende_acl_an(basis: set[str], regeln_von_oben_nach_unten: list[list[AclRegel]]) -> tuple[set[str], set[str]]:
    """Gruppenordner-ACL: die tiefste Regel je Principal mit gesetztem Lese-Bit in der Maske gewinnt."""
    entscheidung: dict[str, bool] = {}
    for ebene in regeln_von_oben_nach_unten:
        for r in ebene:
            if r.maske & LESEN:
                entscheidung[r.key] = bool(r.rechte & LESEN)
    erlaubt = set(basis)
    verboten: set[str] = set()
    for key, darf in entscheidung.items():
        if darf:
            erlaubt.add(key)
            verboten.discard(key)
        else:
            erlaubt.discard(key)
            verboten.add(key)   # z. B. ein einzelner Nutzer innerhalb einer erlaubten Gruppe
    return erlaubt, verboten


@dataclass
class Gruppenordner:
    mount_point: str
    gruppen: dict[str, int]       # gid -> Berechtigungsbits
    acl: bool


class NextcloudRechte:
    """Ermittelt Partitionen mit Cache je Ordner. Ein Aufruf der Share-API je Ordner."""

    def __init__(self, base_url: str, user: str, app_password: str,
                 admin_user: str = "", admin_password: str = "", http: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self._http = http or httpx.Client(auth=(user, app_password), timeout=60.0)
        self._admin = (
            httpx.Client(auth=(admin_user, admin_password), timeout=60.0)
            if admin_user and http is None else None
        )
        self._shares_cache: dict[str, list[dict] | None] = {}
        self._acl_cache: dict[str, list[AclRegel] | None] = {}
        self._gruppenordner: list[Gruppenordner] | None = None

    # --- Rohdaten ------------------------------------------------------------------------

    def _ocs(self, client: httpx.Client, path: str, params: dict) -> dict | None:
        r = client.get(f"{self.base_url}{path}", params={**params, "format": "json"},
                       headers={"OCS-APIRequest": "true", "Accept": "application/json"})
        if r.status_code in (401, 403, 404):
            return None
        r.raise_for_status()
        return r.json()["ocs"]["data"]

    def shares_unter(self, ordner: str) -> list[dict] | None:
        """Freigaben auf den direkten Kindern eines Ordners und auf dem Ordner selbst."""
        if ordner not in self._shares_cache:
            p = "/" + ordner.strip("/")
            api = "/ocs/v2.php/apps/files_sharing/api/v1/shares"
            kinder = self._ocs(self._http, api, {"path": p, "subfiles": "true", "reshares": "true"})
            selbst = self._ocs(self._http, api, {"path": p, "reshares": "true"})
            self._shares_cache[ordner] = None if kinder is None and selbst is None else (kinder or []) + (selbst or [])
        return self._shares_cache[ordner]

    def acl_regeln(self, pfad: str) -> list[AclRegel] | None:
        if pfad not in self._acl_cache:
            url = f"{self.base_url}/remote.php/dav/files/{self.user}/{quote(pfad.strip('/'))}"
            r = self._http.request("PROPFIND", url, content=ACL_PROPFIND,
                                   headers={"Depth": "0", "Content-Type": "application/xml"})
            if r.status_code >= 400:
                self._acl_cache[pfad] = None
            else:
                regeln = parse_acl_list(r.text)
                self._acl_cache[pfad] = next(iter(regeln.values()), [])
        return self._acl_cache[pfad]

    def gruppenordner(self) -> list[Gruppenordner] | None:
        """Nur mit Admin-Zugang lesbar. Ohne ihn: None."""
        if self._gruppenordner is None:
            if self._admin is None:
                return None
            data = self._ocs(self._admin, "/index.php/apps/groupfolders/folders", {})
            if data is None:
                return None
            self._gruppenordner = [
                Gruppenordner(f["mount_point"], {g: int(p) for g, p in (f.get("groups") or {}).items()},
                              bool(f.get("acl")))
                for f in (data.values() if isinstance(data, dict) else data)
            ]
        return self._gruppenordner

    # --- Auswertung ----------------------------------------------------------------------

    def partition(self, datei_pfad: str, root: str) -> Partition:
        teile = PurePosixPath(datei_pfad).parts
        erlaubt: set[str] = set()
        hinweise: list[str] = []
        vollstaendig = True

        # 1 + 2: Freigaben auf der Datei und allen Ordnern bis hinauf zum ersten Pfadelement
        for tiefe in range(len(teile) - 1, 0, -1):
            ordner = "/".join(teile[:tiefe])
            shares = self.shares_unter(ordner)
            if shares is None:
                vollstaendig = False
                hinweise.append(f"Freigaben nicht lesbar: {ordner}")
                continue
            ziel = {"/" + "/".join(teile[:tiefe + 1]), "/" + ordner}
            for s in shares:
                if s.get("path") not in ziel:
                    continue
                if not int(s.get("permissions", 0)) & LESEN:
                    continue
                if int(s["share_type"]) == SHARE_USER and s["share_with"] != self.user:
                    erlaubt.add(f"user:{s['share_with']}")
                elif int(s["share_type"]) == SHARE_GROUP:
                    erlaubt.add(f"group:{s['share_with']}")
                for owner in (s.get("uid_file_owner"), s.get("uid_owner")):
                    if owner and owner != self.user:
                        erlaubt.add(f"user:{owner}")

        # 3: Gruppenordner
        verboten: set[str] = set()
        gf_liste = self.gruppenordner()
        gf = next((g for g in gf_liste or [] if g.mount_point.strip("/") == teile[0]), None)
        if gf is not None:
            erlaubt |= {f"group:{g}" for g, p in gf.gruppen.items() if p & LESEN}
        if (gf is not None and gf.acl) or gf_liste is None:
            ebenen = []
            for tiefe in range(1, len(teile) + 1):
                regeln = self.acl_regeln("/".join(teile[:tiefe]))
                if regeln is None:
                    if gf is not None:
                        vollstaendig = False
                        hinweise.append("ACL nicht lesbar")
                    break
                ebenen.append(regeln)
            if any(ebenen) and gf_liste is None:
                vollstaendig = False
                hinweise.append("Gruppenordner-Gruppen ohne Admin-Zugang unbekannt")
            erlaubt, verboten = wende_acl_an(erlaubt, ebenen)

        if not erlaubt:
            vollstaendig = False
            hinweise.append("keine Berechtigten ermittelt")
        return Partition(frozenset(erlaubt), frozenset(verboten), vollstaendig, "; ".join(hinweise))


class PfadRechte:
    """Pilot-Fallback: Partition aus dem Ordnerpfad (ohne Nextcloud-Rechte)."""

    def __init__(self, depth: int):
        self.depth = depth

    def partition(self, datei_pfad: str, root: str) -> Partition:
        rel = PurePosixPath(datei_pfad).relative_to(root).parts[:-1]
        ordner = "/".join((root, *rel[: self.depth]))
        return Partition(frozenset({f"pfad:{ordner}"}), hinweis="Pfad-Fallback")


def speichere_partition(conn, p: Partition) -> str:
    conn.execute(
        """INSERT INTO rechte_partition (id, erlaubt, verboten, vollstaendig, hinweis)
           VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
        (p.id, sorted(p.erlaubt), sorted(p.verboten), p.vollstaendig, p.hinweis),
    )
    return p.id
