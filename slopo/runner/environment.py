from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import NoReturn

from runner.embedding_services import MODEL_FILE, MODEL_REPOSITORY
from runner.errors import RunError, SkipRun
from runner.paths import Paths


def warn_and_skip(reason: str) -> NoReturn:
    raise SkipRun(f"WARN: Slopo skipped: {reason} Run quality-gates setup to opt in.")


def bootstrap(paths: Paths) -> None:
    if not paths.config.is_file():
        raise RunError(f"No Slopo config found at {paths.config}.")
    if not os.access(paths.venv_python, os.X_OK):
        warn_and_skip("the Slopo environment is absent.")

    expected_prefix = paths.venv_python.parent.parent.resolve()
    if Path(sys.prefix).resolve() != expected_prefix:
        os.execv(
            paths.venv_python,
            [str(paths.venv_python), str(paths.slopo_project / "run.py"), *sys.argv[1:]],
        )


def _resolve_llama() -> Path | None:
    executable = shutil.which("llama")
    if executable:
        return Path(executable)
    for candidate in (
        Path.home() / ".local" / "bin" / "llama",
        Path.home() / ".llama-app" / "llama",
    ):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def validate_setup(paths: Paths) -> Path:
    llama = _resolve_llama()
    if llama is None:
        warn_and_skip("llama.cpp is not installed.")
    if not paths.model_cache.is_dir() or not paths.setup_file.is_file():
        warn_and_skip("the Jina model is absent.")
    if not paths.adapter_script.is_file():
        warn_and_skip("the Jina adapter is absent.")
    if not paths.dependency_lock.is_file():
        raise RunError("The locked SlopO dependency file is absent.")

    setup_lines = set(paths.setup_file.read_text(encoding="utf-8").splitlines())
    required = (
        (
            f"model_repository={MODEL_REPOSITORY}",
            "the configured Jina model is not set up.",
        ),
        (
            f"model_file={MODEL_FILE}",
            "the configured Jina model quantization is not set up.",
        ),
        (
            "embedding_dimensions=256",
            "the configured embedding dimensions are not set up.",
        ),
        ("pooling=last", "the required last-token pooling is not set up."),
        ("task=code2code.query", "the recommended code-to-code task is not set up."),
        ("adapter=local-jina-api", "the local Jina adapter is not set up."),
        ("prompt_cache=disabled", "llama.cpp prompt caching is not disabled."),
    )
    for line, reason in required:
        if line not in setup_lines:
            warn_and_skip(reason)
    return llama


def configure_environment(paths: Paths) -> None:
    os.environ.update(
        {
            "LLAMA_CACHE": str(paths.model_cache),
            "HF_HUB_OFFLINE": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
            "DO_NOT_TRACK": "true",
            "SCARF_NO_ANALYTICS": "true",
        }
    )
    existing_no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    no_proxy = "127.0.0.1,localhost"
    if existing_no_proxy:
        no_proxy = f"{no_proxy},{existing_no_proxy}"
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy
