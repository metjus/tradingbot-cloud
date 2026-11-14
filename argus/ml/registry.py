"""Model registry: versioned storage with promote / rollback.

Lifecycle: candidate -> (validation OK) -> paper_test -> active -> archived.
Phase 5 automates candidate registration and validation gating; promotion
to active after a paper test window is wired up in phase 8 (paper trading),
manual promotion is available meanwhile.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from ..db.database import Database
from ..db.schema import ModelVersion
from .model import SetupScorer, SetupScorerMeta

log = logging.getLogger(__name__)


class ModelRegistry:
    def __init__(self, db: Database, models_dir: Path) -> None:
        self._db = db
        self._dir = models_dir

    def _path(self, name: str, version: int) -> Path:
        return self._dir / name / f"v{version}"

    def next_version(self, name: str) -> int:
        with self._db.session() as s:
            row = s.execute(
                select(ModelVersion.version).where(ModelVersion.name == name)
                .order_by(ModelVersion.version.desc()).limit(1)).scalar_one_or_none()
        return (row or 0) + 1

    def register(self, scorer: SetupScorer, task: str, metrics: dict,
                 status: str = "candidate") -> int:
        name = scorer.meta.name
        version = self.next_version(name)
        scorer.meta.version = version
        scorer.meta.metrics = metrics
        path = self._path(name, version)
        scorer.save(path)
        with self._db.session() as s:
            s.add(ModelVersion(name=name, version=version, task=task,
                               path=str(path), trained_at=datetime.now(timezone.utc),
                               n_samples=scorer.meta.n_samples, metrics=metrics,
                               status=status, active=False))
            s.commit()
        self._db.audit("ml", f"Registered {name} v{version} ({status})", data=metrics)
        return version

    def set_status(self, name: str, version: int, status: str) -> None:
        with self._db.session() as s:
            row = s.execute(select(ModelVersion).where(
                ModelVersion.name == name, ModelVersion.version == version)).scalar_one()
            row.status = status
            s.commit()

    def activate(self, name: str, version: int) -> None:
        with self._db.session() as s:
            rows = s.execute(select(ModelVersion).where(ModelVersion.name == name)).scalars().all()
            found = False
            for r in rows:
                if r.version == version:
                    r.active = True
                    r.status = "active"
                    found = True
                elif r.active:
                    r.active = False
                    r.status = "archived"
            if not found:
                raise ValueError(f"{name} v{version} not registered")
            s.commit()
        self._db.audit("ml", f"Activated {name} v{version}")

    def rollback(self, name: str) -> int | None:
        """Re-activate the most recent archived version."""
        with self._db.session() as s:
            prev = s.execute(select(ModelVersion).where(
                ModelVersion.name == name, ModelVersion.status == "archived")
                .order_by(ModelVersion.version.desc()).limit(1)).scalar_one_or_none()
        if prev is None:
            return None
        self.activate(name, prev.version)
        self._db.audit("ml", f"Rollback: {name} back to v{prev.version}", "warning")
        return prev.version

    def load_active(self, name: str) -> SetupScorer | None:
        with self._db.session() as s:
            row = s.execute(select(ModelVersion).where(
                ModelVersion.name == name, ModelVersion.active.is_(True))).scalar_one_or_none()
        if row is None:
            return None
        try:
            return SetupScorer.load(Path(row.path))
        except Exception as exc:
            log.error("failed to load active model %s v%s: %s", name, row.version, exc)
            self._db.audit("ml", f"Failed to load {name} v{row.version}: {exc}", "error")
            return None

    def list_versions(self, name: str | None = None) -> list[dict]:
        with self._db.session() as s:
            q = select(ModelVersion).order_by(ModelVersion.name, ModelVersion.version.desc())
            if name:
                q = q.where(ModelVersion.name == name)
            rows = s.execute(q).scalars().all()
        return [{"name": r.name, "version": r.version, "task": r.task,
                 "status": r.status, "active": r.active, "n_samples": r.n_samples,
                 "trained_at": r.trained_at.isoformat() if r.trained_at else None,
                 "metrics": r.metrics} for r in rows]
