"""Nextcloud-Konnektor über WebDAV (nur lesend).

- Listet Dateien rekursiv (PROPFIND, Depth 1 je Ordner – Depth infinity ist oft gesperrt).
- Liefert je Datei die stabile fileid (externe ID), den ETag (Version) und einen Deep-Link /f/<fileid>.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Iterator
from urllib.parse import quote, unquote

import httpx

NS = {"d": "DAV:", "oc": "http://owncloud.org/ns", "nc": "http://nextcloud.org/ns"}

PROPFIND_BODY = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
    <d:getetag/><d:getlastmodified/><d:getcontenttype/><d:getcontentlength/><d:resourcetype/>
    <oc:fileid/><oc:permissions/><oc:size/>
  </d:prop>
</d:propfind>"""


@dataclass(frozen=True)
class RemoteFile:
    path: str            # relativ zum Home des Dienstkontos, z. B. "Projekte/Kunde A/Angebot.pdf"
    fileid: str
    etag: str
    mime: str
    size: int
    modified: datetime | None
    is_dir: bool


def parse_multistatus(xml_text: str, dav_prefix: str) -> list[RemoteFile]:
    """Wertet eine PROPFIND-Antwort aus. dav_prefix = URL-Pfad bis zum Home, z. B. /remote.php/dav/files/user/"""
    root = ET.fromstring(xml_text)
    result: list[RemoteFile] = []
    for resp in root.findall("d:response", NS):
        href = unquote(resp.findtext("d:href", default="", namespaces=NS))
        prop = None
        for ps in resp.findall("d:propstat", NS):
            if "200" in (ps.findtext("d:status", default="", namespaces=NS)):
                prop = ps.find("d:prop", NS)
        if prop is None:
            continue
        rel = href.split(dav_prefix, 1)[-1].strip("/")
        is_dir = prop.find("d:resourcetype/d:collection", NS) is not None
        modified_raw = prop.findtext("d:getlastmodified", default="", namespaces=NS)
        size_raw = prop.findtext("d:getcontentlength", namespaces=NS) or prop.findtext("oc:size", namespaces=NS) or "0"
        result.append(
            RemoteFile(
                path=rel,
                fileid=prop.findtext("oc:fileid", default="", namespaces=NS),
                etag=prop.findtext("d:getetag", default="", namespaces=NS).strip('"'),
                mime=prop.findtext("d:getcontenttype", default="", namespaces=NS),
                size=int(size_raw),
                modified=parsedate_to_datetime(modified_raw) if modified_raw else None,
                is_dir=is_dir,
            )
        )
    return result


class NextcloudClient:
    def __init__(self, base_url: str, user: str, app_password: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.user = user
        self.dav_prefix = f"/remote.php/dav/files/{user}/"
        self._http = httpx.Client(auth=(user, app_password), timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    def _url(self, rel_path: str) -> str:
        return f"{self.base_url}{self.dav_prefix}{quote(rel_path.strip('/'))}"

    def deep_link(self, fileid: str) -> str:
        return f"{self.base_url}/f/{fileid}"

    def list_folder(self, rel_path: str) -> list[RemoteFile]:
        r = self._http.request(
            "PROPFIND", self._url(rel_path), content=PROPFIND_BODY,
            headers={"Depth": "1", "Content-Type": "application/xml"},
        )
        r.raise_for_status()
        entries = parse_multistatus(r.text, self.dav_prefix)
        own = rel_path.strip("/")
        return [e for e in entries if e.path != own]

    def walk(self, rel_path: str) -> Iterator[RemoteFile]:
        """Alle Dateien unterhalb von rel_path (rekursiv)."""
        stack = [rel_path]
        while stack:
            folder = stack.pop()
            for entry in self.list_folder(folder):
                if entry.is_dir:
                    stack.append(entry.path)
                else:
                    yield entry

    def download(self, rel_path: str) -> bytes:
        r = self._http.get(self._url(rel_path))
        r.raise_for_status()
        return r.content
