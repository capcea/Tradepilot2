"""SQLite StorePort adapter — schema per SPEC.md §11, SQLAlchemy Core.

Encoding rules (DECISIONS.md M1):
- Timestamps stored as ISO-8601 UTC text (lexicographically ordered).
- Money/prices stored in the spec's REAL columns; every read converts back via
  Decimal(str(x)) which round-trips exactly for these magnitudes. Money is never
  aggregated in SQL — aggregation happens in Python with Decimal.
- M5 bars persist as two candle rows: tf='M5' (bid, carries spread_pts) and
  tf='M5A' (ask). spread_pts is the median spread in integer points.
- audit is append-only, enforced by BEFORE UPDATE/DELETE triggers in the DB
  itself, not just by code discipline.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from core.events import EconEvent, M5Bar
from ports.store import (
    ConfigVersionRow,
    DecisionRow,
    EquitySnapshotRow,
    FillRow,
    IdempotencyViolation,
    OrderIntentRow,
    RiskDayRow,
    SetupRow,
)

UTC = timezone.utc
metadata = sa.MetaData()

instrument = sa.Table(
    "instrument", metadata,
    sa.Column("symbol", sa.Text, primary_key=True),
    sa.Column("broker_symbol", sa.Text),
    sa.Column("digits", sa.Integer),
    sa.Column("point", sa.Float),
    sa.Column("pip", sa.Float),
    sa.Column("contract_size", sa.Float),
    sa.Column("quote_ccy", sa.Text),
    sa.Column("min_lot", sa.Float),
    sa.Column("lot_step", sa.Float),
    sa.Column("max_lot", sa.Float),
)
candle = sa.Table(
    "candle", metadata,
    sa.Column("symbol", sa.Text, primary_key=True),
    sa.Column("tf", sa.Text, primary_key=True),
    sa.Column("ts_utc", sa.Text, primary_key=True),
    sa.Column("o", sa.Float), sa.Column("h", sa.Float),
    sa.Column("l", sa.Float), sa.Column("c", sa.Float),
    sa.Column("tick_vol", sa.Integer),
    sa.Column("spread_pts", sa.Integer),
)
econ_event = sa.Table(
    "econ_event", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("ts_utc", sa.Text),
    sa.Column("currency", sa.Text),
    sa.Column("impact", sa.Text),
    sa.Column("title", sa.Text),
    sa.Column("source", sa.Text),
    sa.Column("fetched_at", sa.Text),
)
setup = sa.Table(
    "setup", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("ts_utc", sa.Text),
    sa.Column("symbol", sa.Text),
    sa.Column("direction", sa.Text),
    sa.Column("range_high", sa.Float), sa.Column("range_low", sa.Float),
    sa.Column("sweep_extreme", sa.Float), sa.Column("reclaim_close", sa.Float),
    sa.Column("features_json", sa.Text),
    sa.Column("status", sa.Text),
)
decision = sa.Table(
    "decision", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("setup_id", sa.Text),
    sa.Column("ts_utc", sa.Text),
    sa.Column("stage", sa.Text),
    sa.Column("passed", sa.Integer),
    sa.Column("reason_code", sa.Text),
    sa.Column("details_json", sa.Text),
)
order_intent = sa.Table(
    "order_intent", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("setup_id", sa.Text),
    sa.Column("ts_utc", sa.Text),
    sa.Column("symbol", sa.Text),
    sa.Column("side", sa.Text),
    sa.Column("lots", sa.Float),
    sa.Column("entry", sa.Float), sa.Column("sl", sa.Float), sa.Column("tp", sa.Float),
    sa.Column("status", sa.Text),
    sa.Column("broker_ticket", sa.Text),
    sa.Column("idempotency_key", sa.Text, unique=True),
)
fill = sa.Table(
    "fill", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("intent_id", sa.Text),
    sa.Column("ts_utc", sa.Text),
    sa.Column("price", sa.Float),
    sa.Column("lots", sa.Float),
    sa.Column("slippage_pips", sa.Float),
    sa.Column("kind", sa.Text),
)
position_snapshot = sa.Table(
    "position_snapshot", metadata,
    sa.Column("ts_utc", sa.Text),
    sa.Column("symbol", sa.Text),
    sa.Column("lots", sa.Float),
    sa.Column("avg_price", sa.Float),
    sa.Column("upl", sa.Float),
    sa.Column("sl", sa.Float),
    sa.Column("tp", sa.Float),
)
equity_snapshot = sa.Table(
    "equity_snapshot", metadata,
    sa.Column("ts_utc", sa.Text, primary_key=True),
    sa.Column("balance", sa.Float),
    sa.Column("equity", sa.Float),
    sa.Column("hwm", sa.Float),
    sa.Column("firm_floor", sa.Float),
    sa.Column("dist_floor", sa.Float),
)
risk_day = sa.Table(
    "risk_day", metadata,
    sa.Column("d", sa.Text, primary_key=True),
    sa.Column("realized", sa.Float),
    sa.Column("fees", sa.Float),
    sa.Column("trades", sa.Integer),
    sa.Column("consec_losses", sa.Integer),
    sa.Column("halted", sa.Integer),
    sa.Column("halt_reason", sa.Text),
    sa.Column("consistency_headroom", sa.Float),
)
config_version = sa.Table(
    "config_version", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("ts_utc", sa.Text),
    sa.Column("author", sa.Text),
    sa.Column("yaml", sa.Text),
    sa.Column("checksum", sa.Text),
    sa.Column("active", sa.Integer),
)
model_registry = sa.Table(
    "model_registry", metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("trained_at", sa.Text),
    sa.Column("metrics_json", sa.Text),
    sa.Column("features_json", sa.Text),
    sa.Column("approved_by", sa.Text),
    sa.Column("active", sa.Integer),
)
audit = sa.Table(
    "audit", metadata,
    sa.Column("seq", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("ts_utc", sa.Text),
    sa.Column("actor", sa.Text),
    sa.Column("event", sa.Text),
    sa.Column("payload_json", sa.Text),
)

_AUDIT_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit "
    "BEGIN SELECT RAISE(ABORT, 'audit table is append-only'); END;",
    "CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit "
    "BEGIN SELECT RAISE(ABORT, 'audit table is append-only'); END;",
)


def _ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime refused; all timestamps must be aware UTC")
    return dt.astimezone(UTC).isoformat()


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def _dec(value: float | int | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class SqliteStore:
    def __init__(self, path: Path | str, points: dict[str, Decimal]):
        self.engine = sa.create_engine(f"sqlite:///{Path(path)}")
        self.points = points
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            for ddl in _AUDIT_TRIGGERS:
                conn.execute(sa.text(ddl))

    # -- market data --------------------------------------------------------

    def upsert_candles(self, bars: Sequence[M5Bar]) -> int:
        if not bars:
            return 0
        rows = []
        for b in bars:
            point = self.points[b.symbol]
            spread_pts = int((b.spread_median / point).to_integral_value(rounding=ROUND_HALF_UP))
            rows.append(dict(symbol=b.symbol, tf="M5", ts_utc=_ts(b.ts_open_utc),
                             o=float(b.bid_o), h=float(b.bid_h), l=float(b.bid_l),
                             c=float(b.bid_c), tick_vol=b.tick_volume, spread_pts=spread_pts))
            rows.append(dict(symbol=b.symbol, tf="M5A", ts_utc=_ts(b.ts_open_utc),
                             o=float(b.ask_o), h=float(b.ask_h), l=float(b.ask_l),
                             c=float(b.ask_c), tick_vol=b.tick_volume, spread_pts=spread_pts))
        stmt = sqlite_insert(candle)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "tf", "ts_utc"],
            set_={c: stmt.excluded[c] for c in ("o", "h", "l", "c", "tick_vol", "spread_pts")},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt, rows)
        return len(bars)

    def get_candles(self, symbol: str, start_utc: datetime, end_utc: datetime) -> list[M5Bar]:
        q = (
            sa.select(candle)
            .where(candle.c.symbol == symbol)
            .where(candle.c.ts_utc >= _ts(start_utc))
            .where(candle.c.ts_utc < _ts(end_utc))
            .order_by(candle.c.ts_utc)
        )
        with self.engine.connect() as conn:
            recs = conn.execute(q).mappings().all()
        bid = {r["ts_utc"]: r for r in recs if r["tf"] == "M5"}
        ask = {r["ts_utc"]: r for r in recs if r["tf"] == "M5A"}
        point = self.points[symbol]
        bars = []
        for ts_text in sorted(bid.keys() & ask.keys()):
            b, a = bid[ts_text], ask[ts_text]
            bars.append(M5Bar(
                symbol=symbol, ts_open_utc=_dt(ts_text),
                bid_o=_dec(b["o"]), bid_h=_dec(b["h"]), bid_l=_dec(b["l"]), bid_c=_dec(b["c"]),
                ask_o=_dec(a["o"]), ask_h=_dec(a["h"]), ask_l=_dec(a["l"]), ask_c=_dec(a["c"]),
                tick_volume=b["tick_vol"],
                spread_median=Decimal(b["spread_pts"]) * point,
            ))
        return bars

    # -- calendar ------------------------------------------------------------

    def upsert_econ_events(self, events: Sequence[EconEvent]) -> int:
        if not events:
            return 0
        seen: dict[str, EconEvent] = {e.id: e for e in events}
        rows = [
            dict(id=e.id, ts_utc=_ts(e.ts_utc), currency=e.currency, impact=e.impact,
                 title=e.title, source=e.source, fetched_at=_ts(e.ts_utc))
            for e in seen.values()
        ]
        stmt = sqlite_insert(econ_event)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={c: stmt.excluded[c] for c in ("ts_utc", "currency", "impact", "title", "source")},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt, rows)
        return len(rows)

    def get_econ_events(self, start_utc: datetime, end_utc: datetime) -> list[EconEvent]:
        q = (
            sa.select(econ_event)
            .where(econ_event.c.ts_utc >= _ts(start_utc))
            .where(econ_event.c.ts_utc < _ts(end_utc))
            .order_by(econ_event.c.ts_utc)
        )
        with self.engine.connect() as conn:
            recs = conn.execute(q).mappings().all()
        return [
            EconEvent(id=r["id"], ts_utc=_dt(r["ts_utc"]), currency=r["currency"],
                      impact=r["impact"], title=r["title"], source=r["source"])
            for r in recs
        ]

    # -- decision trail ------------------------------------------------------

    def insert_setup(self, row: SetupRow) -> None:
        with self.engine.begin() as conn:
            conn.execute(setup.insert().values(
                id=row.id, ts_utc=_ts(row.ts_utc), symbol=row.symbol, direction=row.direction,
                range_high=float(row.range_high), range_low=float(row.range_low),
                sweep_extreme=float(row.sweep_extreme), reclaim_close=float(row.reclaim_close),
                features_json=row.features_json, status=row.status,
            ))

    def update_setup_status(self, setup_id: str, status: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(setup.update().where(setup.c.id == setup_id).values(status=status))

    def get_setup(self, setup_id: str) -> SetupRow | None:
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(setup).where(setup.c.id == setup_id)).mappings().first()
        if r is None:
            return None
        return SetupRow(
            id=r["id"], ts_utc=_dt(r["ts_utc"]), symbol=r["symbol"], direction=r["direction"],
            range_high=_dec(r["range_high"]), range_low=_dec(r["range_low"]),
            sweep_extreme=_dec(r["sweep_extreme"]), reclaim_close=_dec(r["reclaim_close"]),
            features_json=r["features_json"], status=r["status"],
        )

    def get_setups_on(self, d: date) -> list[SetupRow]:
        lo, hi = d.isoformat(), date.fromordinal(d.toordinal() + 1).isoformat()
        with self.engine.connect() as conn:
            recs = conn.execute(
                sa.select(setup).where(setup.c.ts_utc >= lo).where(setup.c.ts_utc < hi)
                .order_by(setup.c.ts_utc)
            ).mappings().all()
        return [
            SetupRow(id=r["id"], ts_utc=_dt(r["ts_utc"]), symbol=r["symbol"],
                     direction=r["direction"], range_high=_dec(r["range_high"]),
                     range_low=_dec(r["range_low"]), sweep_extreme=_dec(r["sweep_extreme"]),
                     reclaim_close=_dec(r["reclaim_close"]),
                     features_json=r["features_json"], status=r["status"])
            for r in recs
        ]

    def get_decisions_on(self, d: date) -> list[DecisionRow]:
        lo, hi = d.isoformat(), date.fromordinal(d.toordinal() + 1).isoformat()
        with self.engine.connect() as conn:
            recs = conn.execute(
                sa.select(decision).where(decision.c.ts_utc >= lo)
                .where(decision.c.ts_utc < hi).order_by(decision.c.ts_utc)
            ).mappings().all()
        return [
            DecisionRow(id=r["id"], setup_id=r["setup_id"], ts_utc=_dt(r["ts_utc"]),
                        stage=r["stage"], passed=bool(r["passed"]),
                        reason_code=r["reason_code"], details_json=r["details_json"])
            for r in recs
        ]

    def insert_decision(self, row: DecisionRow) -> None:
        with self.engine.begin() as conn:
            conn.execute(decision.insert().values(
                id=row.id, setup_id=row.setup_id, ts_utc=_ts(row.ts_utc), stage=row.stage,
                passed=int(row.passed), reason_code=row.reason_code,
                details_json=row.details_json,
            ))

    def get_decisions(self, setup_id: str) -> list[DecisionRow]:
        with self.engine.connect() as conn:
            recs = conn.execute(
                sa.select(decision).where(decision.c.setup_id == setup_id).order_by(decision.c.ts_utc)
            ).mappings().all()
        return [
            DecisionRow(id=r["id"], setup_id=r["setup_id"], ts_utc=_dt(r["ts_utc"]),
                        stage=r["stage"], passed=bool(r["passed"]),
                        reason_code=r["reason_code"], details_json=r["details_json"])
            for r in recs
        ]

    # -- order lifecycle -----------------------------------------------------

    def insert_order_intent(self, row: OrderIntentRow) -> None:
        try:
            with self.engine.begin() as conn:
                conn.execute(order_intent.insert().values(
                    id=row.id, setup_id=row.setup_id, ts_utc=_ts(row.ts_utc),
                    symbol=row.symbol, side=row.side, lots=float(row.lots),
                    entry=float(row.entry), sl=float(row.sl), tp=float(row.tp),
                    status=row.status, broker_ticket=row.broker_ticket,
                    idempotency_key=row.idempotency_key,
                ))
        except sa.exc.IntegrityError as exc:
            raise IdempotencyViolation(
                f"order intent already exists for key {row.idempotency_key!r}"
            ) from exc

    def update_order_intent(
        self, intent_id: str, status: str, broker_ticket: str | None = None
    ) -> None:
        values: dict = {"status": status}
        if broker_ticket is not None:
            values["broker_ticket"] = broker_ticket
        with self.engine.begin() as conn:
            conn.execute(order_intent.update().where(order_intent.c.id == intent_id).values(**values))

    def get_order_intents(self, day: date) -> list[OrderIntentRow]:
        lo, hi = day.isoformat(), (date.fromordinal(day.toordinal() + 1)).isoformat()
        with self.engine.connect() as conn:
            recs = conn.execute(
                sa.select(order_intent)
                .where(order_intent.c.ts_utc >= lo)
                .where(order_intent.c.ts_utc < hi)
                .order_by(order_intent.c.ts_utc)
            ).mappings().all()
        return [
            OrderIntentRow(
                id=r["id"], setup_id=r["setup_id"], ts_utc=_dt(r["ts_utc"]), symbol=r["symbol"],
                side=r["side"], lots=_dec(r["lots"]), entry=_dec(r["entry"]),
                sl=_dec(r["sl"]), tp=_dec(r["tp"]), status=r["status"],
                broker_ticket=r["broker_ticket"], idempotency_key=r["idempotency_key"],
            )
            for r in recs
        ]

    def insert_fill(self, row: FillRow) -> None:
        with self.engine.begin() as conn:
            conn.execute(fill.insert().values(
                id=row.id, intent_id=row.intent_id, ts_utc=_ts(row.ts_utc),
                price=float(row.price), lots=float(row.lots),
                slippage_pips=float(row.slippage_pips), kind=row.kind,
            ))

    # -- risk / equity -------------------------------------------------------

    def insert_equity_snapshot(self, row: EquitySnapshotRow) -> None:
        with self.engine.begin() as conn:
            conn.execute(equity_snapshot.insert().values(
                ts_utc=_ts(row.ts_utc), balance=float(row.balance), equity=float(row.equity),
                hwm=float(row.hwm), firm_floor=float(row.firm_floor),
                dist_floor=float(row.dist_floor),
            ))

    def upsert_risk_day(self, row: RiskDayRow) -> None:
        stmt = sqlite_insert(risk_day).values(
            d=row.d.isoformat(), realized=float(row.realized), fees=float(row.fees),
            trades=row.trades, consec_losses=row.consec_losses, halted=int(row.halted),
            halt_reason=row.halt_reason,
            consistency_headroom=float(row.consistency_headroom),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["d"],
            set_={c: stmt.excluded[c]
                  for c in ("realized", "fees", "trades", "consec_losses", "halted",
                            "halt_reason", "consistency_headroom")},
        )
        with self.engine.begin() as conn:
            conn.execute(stmt)

    def get_risk_day(self, d: date) -> RiskDayRow | None:
        with self.engine.connect() as conn:
            r = conn.execute(sa.select(risk_day).where(risk_day.c.d == d.isoformat())).mappings().first()
        if r is None:
            return None
        return RiskDayRow(
            d=date.fromisoformat(r["d"]), realized=_dec(r["realized"]), fees=_dec(r["fees"]),
            trades=r["trades"], consec_losses=r["consec_losses"], halted=bool(r["halted"]),
            halt_reason=r["halt_reason"], consistency_headroom=_dec(r["consistency_headroom"]),
        )

    # -- governance + audit ---------------------------------------------------

    def insert_config_version(self, row: ConfigVersionRow) -> None:
        with self.engine.begin() as conn:
            conn.execute(config_version.insert().values(
                id=row.id, ts_utc=_ts(row.ts_utc), author=row.author, yaml=row.yaml,
                checksum=row.checksum, active=int(row.active),
            ))

    def get_active_config(self) -> ConfigVersionRow | None:
        with self.engine.connect() as conn:
            r = conn.execute(
                sa.select(config_version).where(config_version.c.active == 1)
            ).mappings().first()
        if r is None:
            return None
        return ConfigVersionRow(id=r["id"], ts_utc=_dt(r["ts_utc"]), author=r["author"],
                                yaml=r["yaml"], checksum=r["checksum"], active=bool(r["active"]))

    def activate_config(self, config_id: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(config_version.update().values(active=0))
            conn.execute(
                config_version.update().where(config_version.c.id == config_id).values(active=1)
            )

    def append_audit(self, ts_utc: datetime, actor: str, event: str, payload_json: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(audit.insert().values(
                ts_utc=_ts(ts_utc), actor=actor, event=event, payload_json=payload_json
            ))
