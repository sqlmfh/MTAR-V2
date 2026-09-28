from __future__ import annotations

from pathlib import Path
import re


def _safe_part(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._")
    return value or "file"


class FileDocumentStore:
    """Filesystem document storage for the automation foundation.

    The directory boundary keeps binary documents out of the job JSON payload.
    A production deployment can replace this adapter with Google Drive or object
    storage while preserving the rest of the application workflow.
    """

    def __init__(self, root: str | Path = "mtar_data") -> None:
        self.root = Path(root)

    def _job_dir(self, job_id: str) -> Path:
        path = self.root / "jobs" / _safe_part(job_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def path(self, job_id: str, category: str, filename: str) -> Path:
        category_dir = self._job_dir(job_id) / _safe_part(category)
        category_dir.mkdir(parents=True, exist_ok=True)
        return category_dir / _safe_part(filename)

    def save_bytes(self, job_id: str, category: str, filename: str, content: bytes) -> Path:
        target = self.path(job_id, category, filename)
        target.write_bytes(content)
        return target

    def read_bytes(self, job_id: str, category: str, filename: str) -> bytes | None:
        target = self.path(job_id, category, filename)
        return target.read_bytes() if target.exists() else None

    def exists(self, job_id: str, category: str, filename: str) -> bool:
        return self.path(job_id, category, filename).exists()

    def delete(self, job_id: str, category: str, filename: str) -> bool:
        target = self.path(job_id, category, filename)
        if not target.exists():
            return False
        target.unlink()
        return True

    def list_files(self, job_id: str) -> list[dict]:
        root = self._job_dir(job_id)
        files = []
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            files.append(
                {
                    "name": path.name,
                    "category": path.parent.name,
                    "path": str(path),
                    "size": path.stat().st_size,
                }
            )
        return files
