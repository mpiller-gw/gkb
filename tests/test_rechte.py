import json

import httpx

from gkb.connectors.nextcloud_rechte import AclRegel, NextcloudRechte, parse_acl_list, wende_acl_an

BASE = "https://cloud.test"


def acl_xml(href: str, regeln: list[tuple[str, str, int, int]]) -> str:
    acls = "".join(
        f"<nc:acl><nc:acl-mapping-type>{t}</nc:acl-mapping-type><nc:acl-mapping-id>{i}</nc:acl-mapping-id>"
        f"<nc:acl-mask>{m}</nc:acl-mask><nc:acl-permissions>{p}</nc:acl-permissions></nc:acl>"
        for t, i, m, p in regeln
    )
    return (f'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns">'
            f"<d:response><d:href>{href}</d:href><d:propstat><d:prop><nc:acl-list>{acls}</nc:acl-list>"
            f"</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>")


def ocs(data) -> httpx.Response:
    return httpx.Response(200, json={"ocs": {"meta": {"status": "ok"}, "data": data}})


class FakeNextcloud:
    def __init__(self, shares=None, acl=None, gruppenordner=None, share_status=200):
        self.shares = shares or {}          # (path, subfiles) -> [shares]
        self.acl = acl or {}                # pfad -> [(typ, id, maske, rechte)]
        self.gruppenordner = gruppenordner
        self.share_status = share_status
        self.aufrufe = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.aufrufe += 1
        if request.url.path.endswith("/files_sharing/api/v1/shares"):
            if self.share_status != 200:
                return httpx.Response(self.share_status)
            key = (request.url.params["path"], request.url.params.get("subfiles") == "true")
            return ocs(self.shares.get(key, []))
        if request.url.path.endswith("/apps/groupfolders/folders"):
            return ocs(self.gruppenordner) if self.gruppenordner is not None else httpx.Response(403)
        if request.method == "PROPFIND":
            pfad = request.url.path.split("/remote.php/dav/files/gkb/", 1)[1]
            from urllib.parse import unquote
            return httpx.Response(207, text=acl_xml(request.url.path, self.acl.get(unquote(pfad), [])))
        return httpx.Response(404)


def rechte(fake: FakeNextcloud, admin: bool = False) -> NextcloudRechte:
    http = httpx.Client(transport=httpx.MockTransport(fake))
    r = NextcloudRechte(BASE, "gkb", "x", http=http)
    if admin:
        r._admin = http
    return r


def test_freigaben_werden_vererbt_und_linkfreigaben_ignoriert():
    fake = FakeNextcloud(shares={
        ("/Projekte", True): [{"path": "/Projekte/Kunde A", "share_type": 1, "share_with": "vertrieb",
                               "permissions": 1, "uid_owner": "anna", "uid_file_owner": "anna"}],
        ("/Projekte/Kunde A", True): [
            {"path": "/Projekte/Kunde A/Angebot.pdf", "share_type": 0, "share_with": "bernd", "permissions": 17},
            {"path": "/Projekte/Kunde A/Angebot.pdf", "share_type": 3, "share_with": None, "permissions": 1},
            {"path": "/Projekte/Kunde A/Angebot.pdf", "share_type": 0, "share_with": "gkb", "permissions": 1},
        ],
    })
    r = rechte(fake)
    p = r.partition("Projekte/Kunde A/Angebot.pdf", "Projekte")
    assert p.erlaubt == {"group:vertrieb", "user:anna", "user:bernd"}
    assert p.vollstaendig
    # gleiche Rechte -> gleiche Partition; Freigaben je Ordner werden nur einmal abgefragt
    vorher = fake.aufrufe
    q = r.partition("Projekte/Kunde A/Konzept.pdf", "Projekte")
    assert q.erlaubt == {"group:vertrieb", "user:anna"} and q.id != p.id
    assert fake.aufrufe - vorher <= 2


def test_nicht_lesbare_freigaben_machen_partition_unvollstaendig():
    p = rechte(FakeNextcloud(share_status=403)).partition("Projekte/Kunde A/a.pdf", "Projekte")
    assert not p.vollstaendig


def test_gruppenordner_mit_acl():
    fake = FakeNextcloud(
        gruppenordner={"1": {"mount_point": "Team", "groups": {"team": 31, "gast": 0}, "acl": True}},
        acl={"Team/Intern": [("group", "team", 1, 0), ("user", "chefin", 1, 1)]},
    )
    r = rechte(fake, admin=True)
    offen = r.partition("Team/Allgemein/a.pdf", "Team")
    assert offen.erlaubt == {"group:team"} and offen.vollstaendig
    intern = r.partition("Team/Intern/b.pdf", "Team")
    assert intern.erlaubt == {"user:chefin"} and intern.verboten == {"group:team"}


def test_acl_ohne_admin_zugang_ist_unvollstaendig():
    fake = FakeNextcloud(acl={"Team/Intern": [("group", "team", 1, 0)]})
    p = rechte(fake).partition("Team/Intern/b.pdf", "Team")
    assert not p.vollstaendig


def test_acl_tiefste_regel_gewinnt():
    oben = [AclRegel("group", "team", 1, 0)]
    unten = [AclRegel("group", "team", 1, 1)]
    erlaubt, verboten = wende_acl_an({"group:team"}, [oben, unten])
    assert erlaubt == {"group:team"} and not verboten
    # Regel ohne Lese-Bit in der Maske ändert nichts
    erlaubt, _ = wende_acl_an({"group:team"}, [[AclRegel("group", "team", 2, 0)]])
    assert erlaubt == {"group:team"}


def test_acl_xml_wird_gelesen():
    regeln = next(iter(parse_acl_list(acl_xml("/x", [("user", "a", 31, 1)])).values()))
    assert (regeln[0].key, regeln[0].maske, regeln[0].rechte) == ("user:a", 31, 1)
    assert json.dumps([r.key for r in regeln]) == '["user:a"]'
