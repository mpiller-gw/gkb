from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SCHEMA = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, row_factory=dict_row, autocommit=False)


def init_db(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()


def quelle_id(conn: psycopg.Connection, art: str, name: str, konfig: dict | None = None) -> int:
    row = conn.execute(
        """INSERT INTO quelle (art, name, konfig) VALUES (%s, %s, %s)
           ON CONFLICT (name) DO UPDATE SET konfig = EXCLUDED.konfig
           RETURNING id""",
        (art, name, psycopg.types.json.Jsonb(konfig or {})),
    ).fetchone()
    conn.commit()
    return row["id"]
