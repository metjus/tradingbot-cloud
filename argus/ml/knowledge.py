"""Export / import of everything the bot has learned (user item 10).

One ``.argus-knowledge.zip`` carries:
  * manifest.json  - format + versions + counts
  * registry.json  - every ML model version with its validation metrics
  * models/...     - the model files themselves
  * trades.json    - closed trades with full entry/exit context (the raw
                     experience: lets ANY future version retrain from it,
                     even when the model format changes)
  * profiles.json  - per-coin strategy calibration
  * settings.json  - runtime settings

Note: the data directory (%APPDATA%\\ARGUS) already survives installer
updates; export is for backups, moving to another PC and keeping a copy
before major version changes.

Security: model files are Python pickles (joblib). Import only bundles you
exported yourself - a crafted file could execute code when loaded.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from .. import __version__
from ..db.database import Database
from ..db.schema import ModelVersion, Trade
from .dataset import FEATURE_SCHEMA_VERSION
from .registry import ModelRegistry
from .training import load_profiles, save_profiles

log = logging.getLogger(__name__)

FORMAT = "argus-knowledge"
FORMAT_VERSION = 1

_TRADE_FIELDS = [
    "client_order_id", "broker", "mode", "symbol", "direction", "strategy", "regime",
    "entry_price", "exit_price", "quantity", "stop_loss", "take_profit", "pl", "fees",
    "slippage", "risk_pct", "rr_planned", "entry_reason", "exit_reason", "status",
    "entry_context", "exit_context",
]


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(s):
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def export_bundle(db: Database, models_dir: Path, runtime=None) -> bytes:
    buf = io.BytesIO()
    with db.session() as s:
        models = s.execute(select(ModelVersion).order_by(ModelVersion.id)).scalars().all()
        trades = s.execute(select(Trade).where(Trade.status == "closed")
                           .order_by(Trade.id)).scalars().all()
    registry, n_files = [], 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for m in models:
            src = Path(m.path)
            rel = f"models/{m.name}/v{m.version}"
            if src.is_dir():
                for f in src.iterdir():
                    if f.is_file():
                        z.write(f, f"{rel}/{f.name}")
                        n_files += 1
            registry.append({"name": m.name, "version": m.version, "task": m.task,
                             "n_samples": m.n_samples, "metrics": m.metrics,
                             "status": m.status, "active": m.active,
                             "trained_at": _iso(m.trained_at), "dir": rel})
        trade_rows = []
        for t in trades:
            row = {f: getattr(t, f) for f in _TRADE_FIELDS}
            row["entry_ts"], row["exit_ts"] = _iso(t.entry_ts), _iso(t.exit_ts)
            trade_rows.append(row)
        profiles = load_profiles(db)
        z.writestr("registry.json", json.dumps(registry, indent=1))
        z.writestr("trades.json", json.dumps(trade_rows, default=str))
        z.writestr("profiles.json", json.dumps(profiles))
        if runtime is not None:
            z.writestr("settings.json", json.dumps(runtime.as_dict(), indent=1))
        z.writestr("manifest.json", json.dumps({
            "format": FORMAT, "format_version": FORMAT_VERSION,
            "argus_version": __version__,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "created": datetime.now(timezone.utc).isoformat(),
            "counts": {"models": len(registry), "model_files": n_files,
                       "trades": len(trade_rows), "profiles": len(profiles)},
        }, indent=1))
    return buf.getvalue()


def _safe_member(name: str) -> bool:
    p = PurePosixPath(name)
    return not p.is_absolute() and ".." not in p.parts and "\\" not in name


def import_bundle(db: Database, models_dir: Path, registry: ModelRegistry, data: bytes,
                  runtime=None, apply_settings: bool = False) -> dict:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("súbor nie je platný ZIP export ARGUS") from exc
    with z:
        names = set(z.namelist())
        if "manifest.json" not in names:
            raise ValueError("chýba manifest.json - nejde o export ARGUS")
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("format") != FORMAT:
            raise ValueError("neznámy formát súboru")
        if manifest.get("format_version", 0) > FORMAT_VERSION:
            raise ValueError("export pochádza z novšej verzie ARGUS - najprv aktualizuj aplikáciu")
        report = {"manifest": manifest, "trades_added": 0, "trades_skipped": 0,
                  "models_added": 0, "model_activated": None, "profiles": 0,
                  "settings_applied": False, "warnings": []}

        # 1) experience: closed trades (dedupe by client_order_id)
        if "trades.json" in names:
            rows = json.loads(z.read("trades.json"))
            with db.session() as s:
                existing = set(s.execute(select(Trade.client_order_id)).scalars().all())
                for row in rows:
                    coid = row.get("client_order_id")
                    if not coid or coid in existing:
                        report["trades_skipped"] += 1
                        continue
                    kw = {f: row.get(f) for f in _TRADE_FIELDS}
                    kw["entry_ts"] = _parse_dt(row.get("entry_ts"))
                    kw["exit_ts"] = _parse_dt(row.get("exit_ts"))
                    kw["fees"] = kw.get("fees") or 0.0
                    s.add(Trade(**kw))
                    existing.add(coid)
                    report["trades_added"] += 1
                s.commit()

        # 2) models: re-numbered into the local registry, never auto-replacing
        schema_ok = manifest.get("feature_schema_version") == FEATURE_SCHEMA_VERSION
        if not schema_ok:
            report["warnings"].append(
                "modely sú z inej verzie príznakov - importované iba ako archív; "
                "obchody z exportu sa použijú pri ďalšom tréningu")
        if "registry.json" in names:
            has_active = any(m["active"] for m in registry.list_versions())
            for entry in json.loads(z.read("registry.json")):
                prefix = entry.get("dir", "").rstrip("/") + "/"
                if not _safe_member(prefix):
                    report["warnings"].append(f"preskočená podozrivá cesta {prefix}")
                    continue
                files = [n for n in names if n.startswith(prefix) and _safe_member(n)
                         and not n.endswith("/")]
                if not files:
                    continue
                version = registry.next_version(entry["name"])
                dest = models_dir / entry["name"] / f"v{version}"
                dest.mkdir(parents=True, exist_ok=True)
                for n in files:
                    target = dest / PurePosixPath(n).name
                    with z.open(n) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out)
                meta_path = dest / "meta.json"
                if meta_path.exists():   # keep meta consistent with new number
                    meta = json.loads(meta_path.read_text())
                    meta["version"] = version
                    meta_path.write_text(json.dumps(meta, indent=2))
                status = "archived" if not schema_ok else (
                    "validated" if entry.get("status") in ("validated", "active", "archived")
                    else entry.get("status", "candidate"))
                with db.session() as s:
                    s.add(ModelVersion(
                        name=entry["name"], version=version, task=entry.get("task", ""),
                        path=str(dest), n_samples=entry.get("n_samples") or 0,
                        metrics={**(entry.get("metrics") or {}),
                                 "imported_from_version": entry.get("version")},
                        status=status, active=False,
                        trained_at=_parse_dt(entry.get("trained_at")) or datetime.now(timezone.utc)))
                    s.commit()
                report["models_added"] += 1
                if entry.get("active") and schema_ok and not has_active:
                    registry.activate(entry["name"], version)
                    report["model_activated"] = f"{entry['name']} v{version}"
                    has_active = True

        # 3) per-coin calibration
        if "profiles.json" in names:
            profiles = json.loads(z.read("profiles.json"))
            if profiles:
                save_profiles(db, profiles)
                report["profiles"] = len(profiles)

        # 4) settings (only on request)
        if apply_settings and runtime is not None and "settings.json" in names:
            stored = json.loads(z.read("settings.json"))
            from ..runtime_settings import SCHEMA
            runtime.update({k: v for k, v in stored.items() if k in SCHEMA})
            report["settings_applied"] = True

    db.audit("ml", "Import znalostí dokončený", data={
        k: v for k, v in report.items() if k != "manifest"})
    return report
