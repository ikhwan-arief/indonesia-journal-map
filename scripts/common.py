"""Shared, dependency-free helpers for the Indonesia journal map pipeline."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]


class PipelineError(RuntimeError):
    """A safe, user-facing pipeline failure."""


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PipelineError(f"Cannot read JSON file {path}: {error}") from None


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)  # mkstemp creates 0600; published files must be world-readable.
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def short_id(value) -> str:
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def require_api_key() -> str:
    key = os.environ.get("OPENALEX_API_KEY", "").strip()
    if not key:
        raise PipelineError("OPENALEX_API_KEY is required; no data was changed.")
    return key
