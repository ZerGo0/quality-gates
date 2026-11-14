import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from seed_embeddings import SeedError, seed_embeddings  # noqa: E402


SCRIPT = Path(__file__).resolve().parents[1] / "seed_embeddings.py"


def create_database(path: Path, *, model: str = "model", dimensions: int = 2) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
            CREATE TABLE metadata (
                id INTEGER PRIMARY KEY,
                embedding_model TEXT NOT NULL,
                embedding_dimensions INTEGER NOT NULL
            );
            CREATE TABLE code_units (body_hash TEXT NOT NULL);
            CREATE TABLE embeddings (body_hash TEXT PRIMARY KEY, embedding BLOB NOT NULL);
            """
        )
        connection.execute("INSERT INTO schema_version VALUES (2)")
        connection.execute(
            "INSERT INTO metadata VALUES (1, ?, ?)", (model, dimensions)
        )


def add_embedding(connection: sqlite3.Connection, body_hash: str, blob: bytes) -> None:
    connection.execute("INSERT INTO embeddings VALUES (?, ?)", (body_hash, blob))


class SeedEmbeddingsTests(unittest.TestCase):
    def test_selectively_copies_only_needed_target_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_path = directory / "source.db"
            target_path = directory / "target.db"
            create_database(source_path)
            create_database(target_path)
            with sqlite3.connect(source_path) as source:
                add_embedding(source, "needed", b"12345678")
                add_embedding(source, "unused", b"abcdefgh")
            with sqlite3.connect(target_path) as target:
                target.executemany(
                    "INSERT INTO code_units VALUES (?)",
                    [("needed",), ("present",), ("missing",)],
                )
                add_embedding(target, "present", b"ABCDEFGH")

            self.assertEqual(seed_embeddings(source_path, target_path), 1)
            with sqlite3.connect(target_path) as target:
                embeddings = dict(target.execute("SELECT body_hash, embedding FROM embeddings"))
            self.assertEqual(
                embeddings,
                {"needed": b"12345678", "present": b"ABCDEFGH"},
            )

    def test_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_path = directory / "source.db"
            target_path = directory / "target.db"
            create_database(source_path)
            create_database(target_path)
            with sqlite3.connect(source_path) as source:
                add_embedding(source, "needed", b"12345678")
            with sqlite3.connect(target_path) as target:
                target.execute("INSERT INTO code_units VALUES (?)", ("needed",))

            self.assertEqual(seed_embeddings(source_path, target_path), 1)
            self.assertEqual(seed_embeddings(source_path, target_path), 0)

    def test_rejects_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target_path = directory / "target.db"
            create_database(target_path)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--source-db",
                    str(directory / "missing.db"),
                    "--target-db",
                    str(target_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("database does not exist", result.stderr)

    def test_rejects_mismatched_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_path = directory / "source.db"
            target_path = directory / "target.db"
            create_database(source_path, model="source-model")
            create_database(target_path, model="target-model")

            with self.assertRaisesRegex(SeedError, "different embedding metadata"):
                seed_embeddings(source_path, target_path)

    def test_rejects_malformed_embedding_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_path = directory / "source.db"
            target_path = directory / "target.db"
            create_database(source_path)
            create_database(target_path)
            with sqlite3.connect(source_path) as source:
                add_embedding(source, "needed", b"too short")
            with sqlite3.connect(target_path) as target:
                target.execute("INSERT INTO code_units VALUES (?)", ("needed",))

            with self.assertRaisesRegex(SeedError, "invalid embedding"):
                seed_embeddings(source_path, target_path)


if __name__ == "__main__":
    unittest.main()
