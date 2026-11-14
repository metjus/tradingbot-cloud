"""Live-trading readiness check (the gate in front of phase 14).

Evaluates the paper-trading track record against explicit criteria. This
is a GATE, not advice: until every check passes, live trading stays
locked. Passing it still does not guarantee profitability - it shows the
system behaved acceptably on a sufficient sample.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from .db.database import Database
from .db.schema import AuditLog, Trade


@dataclass(slots=True)
class Criteria:
    min_trades: int = 100
    min_days: int = 14
    min_profit_factor: float = 1.3
    max_drawdown_pct: float = 6.0
    min_expectancy: float = 0.0        # per trade, after costs
    no_critical_errors_days: int = 7


@dataclass(slots=True)
class Check:
    name: str
    passed: bool
    value: str
    required: str

    def as_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed,
                "value": self.value, "required": self.required}


@dataclass(slots=True)
class ReadinessReport:
    ready: bool = False
    checks: list[Check] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ready": self.ready,
                "checks": [c.as_dict() for c in self.checks],
                "note": ("Splnenie kritérií negarantuje ziskovosť; "
                         "live trading vyžaduje ešte výslovné povolenie používateľa.")}


def evaluate_readiness(db: Database, start_equity: float,
                       criteria: Criteria | None = None) -> ReadinessReport:
    c = criteria or Criteria()
    rep = ReadinessReport()
    with db.session() as s:
        trades = s.execute(
            select(Trade).where(Trade.status == "closed", Trade.mode == "paper")
            .order_by(Trade.exit_ts)).scalars().all()
        since = datetime.now(timezone.utc) - timedelta(days=c.no_critical_errors_days)
        criticals = s.execute(
            select(AuditLog).where(AuditLog.severity == "critical",
                                   AuditLog.ts >= since)).scalars().all()

    n = len(trades)
    rep.checks.append(Check("Počet uzatvorených paper obchodov", n >= c.min_trades,
                            str(n), f">= {c.min_trades}"))

    if trades:
        first = trades[0].exit_ts or trades[0].entry_ts
        last = trades[-1].exit_ts or trades[-1].entry_ts
        days = (last - first).days if first and last else 0
    else:
        days = 0
    rep.checks.append(Check("Dĺžka paper obdobia (dni)", days >= c.min_days,
                            str(days), f">= {c.min_days}"))

    pls = [t.pl or 0.0 for t in trades]
    gross_win = sum(p for p in pls if p > 0)
    gross_loss = abs(sum(p for p in pls if p <= 0))
    pf = gross_win / gross_loss if gross_loss > 0 else (float("inf") if gross_win else 0.0)
    rep.checks.append(Check("Profit factor", n > 0 and pf >= c.min_profit_factor,
                            f"{pf:.2f}" if n else "—", f">= {c.min_profit_factor}"))

    expectancy = (sum(pls) / n) if n else 0.0
    rep.checks.append(Check("Expectancy na obchod (po nákladoch)",
                            n > 0 and expectancy > c.min_expectancy,
                            f"{expectancy:+.2f} €", f"> {c.min_expectancy} €"))

    equity = start_equity
    peak = equity
    max_dd = 0.0
    for p in pls:
        equity += p
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)
    rep.checks.append(Check("Max drawdown", n > 0 and max_dd <= c.max_drawdown_pct,
                            f"{max_dd:.2f}%" if n else "—", f"<= {c.max_drawdown_pct}%"))

    rep.checks.append(Check(
        f"Žiadne kritické chyby za {c.no_critical_errors_days} dní",
        len(criticals) == 0, str(len(criticals)), "0"))

    rep.ready = all(ch.passed for ch in rep.checks)
    return rep
