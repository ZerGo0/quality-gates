from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import NoReturn, Sequence

from runner.errors import RunError
from runner.paths import Paths


SERVER_HOST = "127.0.0.1"
SERVER_PORT = 17997
ADAPTER_PORT = 17998
HEALTH_POLL_INTERVAL = 0.05
SERVER_HEALTH_TIMEOUT = 120.0
ADAPTER_HEALTH_TIMEOUT = 30.0
MODEL_ALIAS = "jina-code-embeddings-0.5b"
MODEL_REPOSITORY = "jinaai/jina-code-embeddings-0.5b-GGUF"
MODEL_FILE = "jina-code-embeddings-0.5b-Q8_0.gguf"
STOP_TIMEOUT = 5


class EmbeddingServices:
    def __init__(self, paths: Paths, llama: Path) -> None:
        self.paths = paths
        self.llama = llama
        self.server: subprocess.Popen[bytes] | None = None
        self.adapter: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        if self.server is not None or self.adapter is not None:
            return
        self.server = self._spawn(
            [
                str(self.llama),
                "serve",
                "--offline",
                "--embedding",
                "--hf-repo",
                MODEL_REPOSITORY,
                "--hf-file",
                MODEL_FILE,
                "--alias",
                MODEL_ALIAS,
                "--host",
                SERVER_HOST,
                "--port",
                str(SERVER_PORT),
                "--ctx-size",
                "8192",
                "--ubatch-size",
                "8192",
                "--pooling",
                "last",
                "--embd-normalize",
                "2",
                "--cache-ram",
                "0",
                "--no-webui",
            ],
            self.paths.server_log,
        )
        self._wait_until_ready(
            self.server,
            f"http://{SERVER_HOST}:{SERVER_PORT}/health",
            SERVER_HEALTH_TIMEOUT,
        )
        self.adapter = self._spawn(
            [
                sys.executable,
                str(self.paths.adapter_script),
                "--host",
                SERVER_HOST,
                "--port",
                str(ADAPTER_PORT),
                "--upstream-url",
                f"http://{SERVER_HOST}:{SERVER_PORT}/v1/embeddings",
            ],
            self.paths.adapter_log,
        )
        self._wait_until_ready(
            self.adapter,
            f"http://{SERVER_HOST}:{ADAPTER_PORT}/health",
            ADAPTER_HEALTH_TIMEOUT,
        )

    @staticmethod
    def _spawn(command: Sequence[str], log_path: Path) -> subprocess.Popen[bytes]:
        with log_path.open("wb") as log:
            return subprocess.Popen(
                command,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def _wait_until_ready(
        self, process: subprocess.Popen[bytes], health_url: str, timeout: float
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self._raise_startup_error()
            try:
                with urllib.request.urlopen(health_url, timeout=0.2) as response:
                    if 200 <= response.status < 300:
                        return
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(HEALTH_POLL_INTERVAL)
        self._raise_startup_error()

    def _raise_startup_error(self) -> NoReturn:
        details = [
            "Slopo failed because the local embedding service did not become ready."
        ]
        for path in (self.paths.server_log, self.paths.adapter_log):
            if path.is_file():
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                details.extend(lines[-80:])
        raise RunError("\n".join(details))

    @staticmethod
    def _signal_group(process: subprocess.Popen[bytes], signum: int) -> None:
        with suppress(ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signum)

    def stop(self) -> None:
        for process in (self.adapter, self.server):
            if process is None or process.poll() is not None:
                continue
            try:
                self._signal_group(process, signal.SIGTERM)
                process.wait(timeout=STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self._signal_group(process, signal.SIGKILL)
                process.wait()
        self.adapter = None
        self.server = None
