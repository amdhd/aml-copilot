"""Fixed guidance corpus -> pgvector.

Embeddings run locally: node 3 embeds a query on every case, so an embedding API
would put a rate limit in the path of every investigation. It is also what a bank
would run in-VPC.
"""

import re
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from sentence_transformers import SentenceTransformer

from db import DSN, GUIDANCE_INDEX, GUIDANCE_SCHEMA

CORPUS = Path("data/corpus")
MODEL = "Qwen/Qwen3-Embedding-0.6B"
TARGET_CHARS = 1400          # a guidance provision, kept whole


def _read(path: Path) -> str:
    if path.suffix == ".pdf":
        from pypdf import PdfReader
        return "\n\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
    return path.read_text(encoding="utf-8", errors="ignore")


def chunk(text: str) -> list[str]:
    """Merge paragraphs up to TARGET_CHARS. Splitting a provision mid-sentence is
    how a model ends up applying half a rule."""
    text = re.sub(r"[ \t]+", " ", text)
    out, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if len(para) < 40:                       # page numbers, headers
            continue
        if len(buf) + len(para) > TARGET_CHARS and buf:
            out.append(buf.strip())
            buf = ""
        buf += para + "\n"
    if buf.strip():
        out.append(buf.strip())
    return out


def main():
    files = sorted(p for p in CORPUS.iterdir()
                   if p.suffix in {".pdf", ".txt", ".md"})
    if not files:
        raise SystemExit(f"no documents in {CORPUS}")

    texts, sources, indexes = [], [], []
    for path in files:
        pieces = chunk(_read(path))
        print(f"{path.name}: {len(pieces)} chunks")
        texts += pieces
        sources += [path.stem] * len(pieces)
        indexes += list(range(len(pieces)))

    print(f"embedding {len(texts)} chunks with {MODEL}")
    vectors = SentenceTransformer(MODEL).encode(texts, batch_size=8,
                                                show_progress_bar=False)

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(GUIDANCE_SCHEMA)
        register_vector(conn)
        conn.execute("TRUNCATE guidance")
        with conn.cursor().copy(
                "COPY guidance (source, chunk_ix, text, embedding) FROM STDIN "
                "WITH (FORMAT BINARY)") as copy:
            copy.set_types(["text", "integer", "text", "vector"])
            for source, ix, text, vector in zip(sources, indexes, texts, vectors):
                copy.write_row((source, ix, text, vector))
        conn.execute(GUIDANCE_INDEX)
        for row in conn.execute("SELECT source, count(*) FROM guidance "
                                "GROUP BY source ORDER BY source"):
            print(f"  {row[0]}: {row[1]} chunks")


if __name__ == "__main__":
    main()
