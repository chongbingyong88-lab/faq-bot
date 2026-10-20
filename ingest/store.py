"""Postgres + pgvector storage.

One table, sliced by `site`, so a single database can hold every
prospect you demo to.
"""
from __future__ import annotations

import os

import psycopg
from pgvector.psycopg import register_vector

from .chunk import Chunk
from .embed import EMBED_DIM

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://faqbot:faqbot@localhost:5433/faqbot"
)

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS chunks (
    id        BIGSERIAL PRIMARY KEY,
    site      TEXT NOT NULL,
    url       TEXT NOT NULL,
    title     TEXT DEFAULT '',
    heading   TEXT DEFAULT '',
    content   TEXT NOT NULL,
    embedding vector({EMBED_DIM}) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chunks_site_idx ON chunks (site);
CREATE INDEX IF NOT EXISTS chunks_vec_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);
"""


def connect() -> psycopg.Connection:
    conn = psycopg.connect(DATABASE_URL, autocommit=True)
    register_vector(conn)
    return conn


def init_db(conn: psycopg.Connection) -> None:
    """Create the schema, rebuilding it if the vector width changed.

    Switching embedding model changes the dimension, and an existing
    vector(N) column silently survives CREATE TABLE IF NOT EXISTS — every
    insert would then fail. Chunks are regenerable from the page cache, so
    dropping is safe and beats a confusing error.
    """
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
    current = conn.execute("""
        SELECT a.atttypmod FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relname = 'chunks' AND a.attname = 'embedding'
    """).fetchone()
    if current and current[0] not in (EMBED_DIM, -1):
        print(f"  embedding dimension changed {current[0]} -> {EMBED_DIM}; "
              f"rebuilding the chunks table", flush=True)
        conn.execute("DROP TABLE chunks")
    conn.execute(SCHEMA)


def replace_site(
    conn: psycopg.Connection,
    site: str,
    chunks: list[Chunk],
    vectors: list[list[float]],
) -> int:
    """Wipe and reinsert a site. Re-ingesting is idempotent."""
    with conn.transaction():
        conn.execute("DELETE FROM chunks WHERE site = %s", (site,))
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO chunks (site, url, title, heading, content, embedding)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                [
                    (site, c.url, c.title, c.heading, c.text, v)
                    for c, v in zip(chunks, vectors)
                ],
            )
    return len(chunks)


def search(
    conn: psycopg.Connection,
    site: str,
    query_vec: list[float],
    limit: int = 20,
) -> list[dict]:
    rows = conn.execute(
        """SELECT url, title, heading, content,
                  1 - (embedding <=> %s::vector) AS similarity
           FROM chunks
           WHERE site = %s
           ORDER BY embedding <=> %s::vector
           LIMIT %s""",
        (query_vec, site, query_vec, limit),
    ).fetchall()

    return [
        {
            "url": r[0],
            "title": r[1],
            "heading": r[2],
            "content": r[3],
            "similarity": float(r[4]),
        }
        for r in rows
    ]


def list_sites(conn: psycopg.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT site, COUNT(*) FROM chunks GROUP BY site ORDER BY site"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]
