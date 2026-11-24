from __future__ import annotations

from contextlib import chdir
from dataclasses import replace
from pathlib import Path

from runner.console import emit
from runner.embedding_services import ADAPTER_PORT, SERVER_HOST, EmbeddingServices
from runner.errors import RunError
from runner.paths import Paths


def load_config(
    paths: Paths, working_directory: Path, database: Path, report_dir: Path
):
    from slopo.config import load_config

    with chdir(working_directory):
        config = load_config(paths.config)
    return replace(
        config, db_file=database, report_dir=report_dir, ignore_file=paths.ignore_file
    )


def validate_current_config(config: object) -> None:
    expected_api_base = f"http://{SERVER_HOST}:{ADAPTER_PORT}/v1"
    actual_api_base = config.embedding_params.get("api_base")
    if actual_api_base != expected_api_base:
        raise RunError(
            "Slopo runtime must use the repository-local embedding adapter at "
            f"{expected_api_base}, got {actual_api_base!r}"
        )
    if not config.source_dir.is_dir():
        raise RunError(f"SlopO source directory is missing: {config.source_dir}")


def index(config: object) -> int:
    from slopo.db import create_db, open_db
    from slopo.embedding.db import count_unembedded_units
    from slopo.indexing.command import run_index

    connection = open_db(config) if config.db_file.exists() else create_db(config)
    try:
        run_index(connection, config, print)
        return count_unembedded_units(connection)
    finally:
        connection.close()


def count_unembedded(config: object) -> int:
    from slopo.db import open_db
    from slopo.embedding.db import count_unembedded_units

    connection = open_db(config)
    try:
        return count_unembedded_units(connection)
    finally:
        connection.close()


def embed(config: object, missing_count: int, services: EmbeddingServices) -> None:
    from slopo.db import open_db
    from slopo.embedding.command import run_embed

    if missing_count == 0:
        emit("No new code units to embed.")
        return
    services.start()
    connection = open_db(config)
    try:
        run_embed(connection, config, print)
    finally:
        connection.close()


def analyze(config: object) -> None:
    from slopo.db import open_db
    from slopo.embedding.db import count_unembedded_units
    from slopo.result.analysis.command import run_analyze
    from slopo.result.report.filesystem import write_analyze_report

    connection = open_db(config)
    try:
        if count_unembedded_units(connection) > 0:
            raise RunError(
                "Some code units have no embeddings after embedding completed."
            )
        result = run_analyze(connection, config, print)
        if result is not None:
            write_analyze_report(result.clusters, result.units, config.report_dir)
            emit(f"Report written to {config.report_dir} directory.")
    finally:
        connection.close()


def clear_reports(report_dir: Path) -> None:
    from changed_files_gate import clear_generated_reports

    clear_generated_reports(report_dir)


def refresh_report(config: object, report_dir: Path) -> None:
    clear_reports(report_dir)
    analyze(config)
