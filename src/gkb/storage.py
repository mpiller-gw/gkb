"""Unveränderte Ablage der Originale, adressiert über den Inhalts-Hash."""
import hashlib
from pathlib import Path


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def store_original(base_dir: str, data: bytes, suffix: str = "") -> tuple[str, str]:
    """Speichert die Bytes unter <base>/<aa>/<hash><suffix>. Gibt (hash, pfad) zurück."""
    digest = sha256(data)
    target = Path(base_dir) / digest[:2] / f"{digest}{suffix}"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    return digest, str(target)
