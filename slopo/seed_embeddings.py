"""Reuse compatible SlopO embeddings from one existing database in another."""

from __future__ import annotations

import argparse
import os
import sqlite3
import stat
import sys
from collections.abc import Iterable
from pathlib import Path


# Keep room under SQLite's historical 999-variable limit.
_BATCH_SIZE = 900
_REQUIRED_COLUMNS = {
    "schema_version": {"version"},
    "metadata": {"id", "embedding_model", "embedding_dimensions"},
    "code_units": {"body_hash"},
    "embeddings": {"body_hash", "embedding"},
}


class SeedError(Exception):
    """Raised when a database cannot safely provide reusable embeddings."""


def existing_database(value: str) -> Path:
    path = Path(value)
    try:
        file_status = path.lstat()
    except FileNotFoundError as error:
        raise argparse.ArgumentTypeError(f"database does not exist: {path}") from error
    if stat.S_ISLNK(file_status.st_mode) or not stat.S_ISREG(file_status.st_mode):
        raise argparse.ArgumentTypeError(
            f"database must be a regular, non-symlink file: {path}"
        )
    return path


def _validate_tables(connection: sqlite3.Connection, label: str) -> None:
    for table, columns in _REQUIRED_COLUMNS.items():
        found = {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not columns <= found:
            raise SeedError(f"{label} database has an invalid or missing {table} table")


def _load_metadata(connection: sqlite3.Connection, label: str) -> tuple[int, str, int]:
    _validate_tables(connection, label)
    versions = connection.execute("SELECT version FROM schema_version").fetchall()
    if len(versions) != 1 or not isinstance(versions[0][0], int):
        raise SeedError(f"{label} database has invalid schema version metadata")
    metadata = connection.execute(
        "SELECT embedding_model, embedding_dimensions FROM metadata WHERE id = ?",
        (1,),
    ).fetchall()
    if len(metadata) != 1:
        raise SeedError(f"{label} database has invalid embedding metadata")
    model, dimensions = metadata[0]
    if (
        not isinstance(model, str)
        or not model
        or not isinstance(dimensions, int)
        or dimensions <= 0
    ):
        raise SeedError(f"{label} database has invalid embedding metadata")
    return versions[0][0], model, dimensions


def _validate_embedding(
    blob: object, dimensions: int, label: str, body_hash: object
) -> bytes:
    if not isinstance(blob, bytes) or len(blob) != dimensions * 4:
        raise SeedError(
            f"{label} database has an invalid embedding for body hash {body_hash!r}"
        )
    return blob


def _validate_embedding_blobs(
    connection: sqlite3.Connection, dimensions: int, label: str
) -> None:
    for body_hash, blob in connection.execute("SELECT body_hash, embedding FROM embeddings"):
        _validate_embedding(blob, dimensions, label, body_hash)


def _batches(values: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(values), _BATCH_SIZE):
        yield values[start : start + _BATCH_SIZE]


def seed_embeddings(source_db: Path, target_db: Path) -> int:
    """Copy source vectors needed by target code units and return their count."""
    if os.path.samefile(source_db, target_db):
        raise SeedError("source and target databases must be different files")

    try:
        with sqlite3.connect(source_db) as source, sqlite3.connect(target_db) as target:
            source_version, source_model, source_dimensions = _load_metadata(
                source, "source"
            )
            target_version, target_model, target_dimensions = _load_metadata(
                target, "target"
            )
            if source_version != target_version:
                raise SeedError("source and target databases use different schema versions")
            if source_model != target_model or source_dimensions != target_dimensions:
                raise SeedError("source and target databases use different embedding metadata")

            _validate_embedding_blobs(source, source_dimensions, "source")
            _validate_embedding_blobs(target, target_dimensions, "target")
            needed_hashes = [
                row[0]
                for row in target.execute(
                    """
                    SELECT DISTINCT code_units.body_hash
                    FROM code_units
                    WHERE NOT EXISTS (
                        SELECT 1 FROM embeddings
                        WHERE embeddings.body_hash = code_units.body_hash
                    )
                    """
                )
            ]
            copied = 0
            for hashes in _batches(needed_hashes):
                placeholders = ", ".join("?" for _ in hashes)
                rows = source.execute(
                    f"SELECT body_hash, embedding FROM embeddings WHERE body_hash IN ({placeholders})",
                    hashes,
                ).fetchall()
                values = [
                    (
                        body_hash,
                        _validate_embedding(
                            blob, source_dimensions, "source", body_hash
                        ),
                    )
                    for body_hash, blob in rows
                ]
                target.executemany(
                    "INSERT INTO embeddings (body_hash, embedding) VALUES (?, ?)", values
                )
                copied += len(values)
            target.commit()
            return copied
    except sqlite3.Error as error:
        raise SeedError(f"could not read SlopO databases: {error}") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Copy reusable SlopO embeddings into an existing database."
    )
    parser.add_argument("--source-db", required=True, type=existing_database)
    parser.add_argument("--target-db", required=True, type=existing_database)
    arguments = parser.parse_args(argv)
    try:
        copied = seed_embeddings(arguments.source_db, arguments.target_db)
    except SeedError as error:
        print(f"Could not reuse embeddings: {error}", file=sys.stderr)
        return 1
    print(f"Reused {copied} embedding{'s' if copied != 1 else ''}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
