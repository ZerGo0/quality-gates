from __future__ import annotations

import sys
from typing import TextIO


def emit(message: str, stream: TextIO | None = None) -> None:
    (stream or sys.stdout).write(f"{message}\n")
