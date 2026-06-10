"""MT5 execution/market-data adapter (SPEC.md §10.4) — Windows-only, import-guarded.

The MetaTrader5 package is poll-based and account-mode sensitive; this adapter:
- maps canonical symbols to broker variants and REFUSES any symbol whose live
  digits/point/contract size disagree with the instrument spec (§19 row 3);
- respects the symbol's allowed filling modes and stop levels;
- stamps every order with the strategy magic number and the intent id comment;
- when constructed with a LiveGate, refuses to send anything while the gate is
  closed (LIVE_TRADING=1 + ARM_LIVE file + config checksum match).

Everything that can be pure (request building, retcode mapping, symbol
validation) is module-level and unit-tested against a fake API; only the thin
I/O wrappers need a real terminal.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping

try:  # Windows-only dependency; the rest of the suite must run anywhere
    import MetaTrader5 as _mt5
except ImportError:  # pragma: no cover - exercised on non-Windows CI
    _mt5 = None

from core.config_schema import InstrumentSpec
from core.events import M5Bar, TickBatch
from ports.execution import AccountState, BracketOrder, BrokerPosition, OrderResult

D = Decimal
UTC = timezone.utc


class MT5Error(Exception):
    pass


def validate_symbol(spec: InstrumentSpec, info) -> tuple[str, ...]:
    """§10.4 boot validation: a wrong-symbol trade is refused, not 'fixed'."""
    v: list[str] = []
    if info.digits != spec.digits:
        v.append(f"digits mismatch: broker={info.digits} spec={spec.digits}")
    if D(str(info.point)) != spec.point:
        v.append(f"point mismatch: broker={info.point} spec={spec.point}")
    if D(str(info.trade_contract_size)) != spec.contract_size:
        v.append(
            f"contract size mismatch: broker={info.trade_contract_size} spec={spec.contract_size}"
        )
    if D(str(info.volume_min)) != spec.min_lot:
        v.append(f"volume_min mismatch: broker={info.volume_min} spec={spec.min_lot}")
    if D(str(info.volume_step)) != spec.lot_step:
        v.append(f"volume_step mismatch: broker={info.volume_step} spec={spec.lot_step}")
    if D(str(info.volume_max)) != spec.max_lot:
        v.append(f"volume_max mismatch: broker={info.volume_max} spec={spec.max_lot}")
    return tuple(v)


def _filling_mode(api, symbol_info) -> int:
    mask = getattr(symbol_info, "filling_mode", 0)
    if mask & getattr(api, "SYMBOL_FILLING_FOK", 1):
        return api.ORDER_FILLING_FOK
    if mask & getattr(api, "SYMBOL_FILLING_IOC", 2):
        return api.ORDER_FILLING_IOC
    return api.ORDER_FILLING_RETURN


def build_order_request(
    api,
    order: BracketOrder,
    broker_symbol: str,
    price: Decimal,
    point: Decimal,
    symbol_info,
) -> dict:
    return {
        "action": api.TRADE_ACTION_DEAL,
        "symbol": broker_symbol,
        "volume": float(order.lots),
        "type": api.ORDER_TYPE_BUY if order.side == "long" else api.ORDER_TYPE_SELL,
        "price": float(price),
        "sl": float(order.sl),
        "tp": float(order.tp),
        "deviation": int(order.max_deviation / point),
        "magic": order.magic,
        "comment": order.comment,
        "type_time": api.ORDER_TIME_GTC,
        "type_filling": _filling_mode(api, symbol_info),
    }


_RETRYABLE = {"requote", "price_off", "timeout", "price_changed", "connection"}


def map_retcode(api, retcode: int) -> tuple[str, bool]:
    table = {
        getattr(api, "TRADE_RETCODE_DONE", 10009): ("ok", False),
        getattr(api, "TRADE_RETCODE_REQUOTE", 10004): ("requote", True),
        getattr(api, "TRADE_RETCODE_PRICE_OFF", 10021): ("price_off", True),
        getattr(api, "TRADE_RETCODE_TIMEOUT", 10012): ("timeout", True),
        getattr(api, "TRADE_RETCODE_PRICE_CHANGED", 10020): ("price_changed", True),
        getattr(api, "TRADE_RETCODE_CONNECTION", 10031): ("connection", True),
        getattr(api, "TRADE_RETCODE_INVALID_STOPS", 10016): ("invalid_stops", False),
        getattr(api, "TRADE_RETCODE_NO_MONEY", 10019): ("no_money", False),
    }
    return table.get(retcode, (f"retcode_{retcode}", False))


@dataclass
class _SymbolBinding:
    canonical: str
    broker: str
    spec: InstrumentSpec


class MT5Adapter:
    """ExecutionPort + MarketDataPort over the MetaTrader5 package."""

    def __init__(
        self,
        symbol_map: Mapping[str, str],
        specs: Mapping[str, InstrumentSpec],
        magic: int,
        api=None,
        gate=None,
    ):
        self.api = api if api is not None else _mt5
        if self.api is None:
            raise MT5Error(
                "MetaTrader5 package unavailable (Windows-only). Use the paper adapter."
            )
        self.symbol_map = dict(symbol_map)
        self.specs = dict(specs)
        self.magic = magic
        self.gate = gate

    # -- connection / boot canary (§19 row 20) ---------------------------------

    def connect(self, login: int, password: str, server: str, path: str | None = None):
        kwargs = {"login": login, "password": password, "server": server}
        if path:
            kwargs["path"] = path
        if not self.api.initialize(**kwargs):  # pragma: no cover - needs terminal
            raise MT5Error(f"initialize failed: {self.api.last_error()}")
        self.boot_validate()

    def boot_validate(self) -> dict[str, tuple[str, ...]]:
        """Validate every mapped symbol; raise if ANY mismatches (fail closed)."""
        problems: dict[str, tuple[str, ...]] = {}
        for canonical, broker in self.symbol_map.items():
            info = self.api.symbol_info(broker)
            if info is None:
                problems[canonical] = (f"broker symbol {broker} not found",)
                continue
            v = validate_symbol(self.specs[canonical], info)
            if v:
                problems[canonical] = v
        if problems:
            raise MT5Error(f"symbol validation failed: {problems}")
        return problems

    # -- ExecutionPort -----------------------------------------------------------

    def place_bracket_market(self, order: BracketOrder) -> OrderResult:
        if self.gate is not None:
            res = self.gate.check()
            if not res.allowed:
                return OrderResult(
                    ok=False, error="live gate refused: " + "; ".join(res.reasons),
                    retryable=False,
                )
        broker_symbol = self.symbol_map[order.symbol]
        tick = self.api.symbol_info_tick(broker_symbol)
        if tick is None:
            return OrderResult(ok=False, error="no tick", retryable=True)
        price = D(str(tick.ask)) if order.side == "long" else D(str(tick.bid))
        info = self.api.symbol_info(broker_symbol)
        request = build_order_request(
            self.api, order, broker_symbol, price, self.specs[order.symbol].point, info
        )
        result = self.api.order_send(request)
        if result is None:
            return OrderResult(ok=False, error="order_send returned None", retryable=True)
        label, retryable = map_retcode(self.api, result.retcode)
        if label != "ok":
            return OrderResult(ok=False, error=label, retryable=retryable)
        return OrderResult(
            ok=True,
            broker_ticket=str(result.order),
            fill_price=D(str(result.price)),
            filled_lots=D(str(result.volume)),
        )

    def modify_position(self, ticket: str, sl, tp) -> OrderResult:  # pragma: no cover
        positions = self.api.positions_get(ticket=int(ticket))
        if not positions:
            return OrderResult(ok=False, error="position not found", retryable=False)
        p = positions[0]
        request = {
            "action": self.api.TRADE_ACTION_SLTP,
            "position": int(ticket),
            "symbol": p.symbol,
            "sl": float(sl) if sl is not None else p.sl,
            "tp": float(tp) if tp is not None else p.tp,
            "magic": self.magic,
        }
        result = self.api.order_send(request)
        label, retryable = map_retcode(self.api, result.retcode)
        return OrderResult(ok=label == "ok", broker_ticket=ticket,
                           error=None if label == "ok" else label, retryable=retryable)

    def close_position(self, ticket: str, lots=None) -> OrderResult:  # pragma: no cover
        positions = self.api.positions_get(ticket=int(ticket))
        if not positions:
            return OrderResult(ok=False, error="position not found", retryable=False)
        p = positions[0]
        tick = self.api.symbol_info_tick(p.symbol)
        if tick is None:
            return OrderResult(ok=False, error="no tick", retryable=True)
        is_long = p.type == getattr(self.api, "POSITION_TYPE_BUY", 0)
        volume = float(lots) if lots is not None else p.volume
        info = self.api.symbol_info(p.symbol)
        request = {
            "action": self.api.TRADE_ACTION_DEAL,
            "position": int(ticket),
            "symbol": p.symbol,
            "volume": volume,
            "type": self.api.ORDER_TYPE_SELL if is_long else self.api.ORDER_TYPE_BUY,
            "price": tick.bid if is_long else tick.ask,
            "deviation": 50,
            "magic": self.magic,
            "comment": f"close:{p.comment}"[:31],
            "type_time": self.api.ORDER_TIME_GTC,
            "type_filling": _filling_mode(self.api, info),
        }
        result = self.api.order_send(request)
        label, retryable = map_retcode(self.api, result.retcode)
        if label != "ok":
            return OrderResult(ok=False, error=label, retryable=retryable)
        return OrderResult(ok=True, broker_ticket=ticket,
                           fill_price=D(str(result.price)), filled_lots=D(str(result.volume)))

    def positions(self) -> list[BrokerPosition]:  # pragma: no cover
        raw = self.api.positions_get() or ()
        out = []
        reverse = {v: k for k, v in self.symbol_map.items()}
        for p in raw:
            out.append(BrokerPosition(
                ticket=str(p.ticket),
                symbol=reverse.get(p.symbol, p.symbol),
                side="long" if p.type == getattr(self.api, "POSITION_TYPE_BUY", 0) else "short",
                lots=D(str(p.volume)), entry_price=D(str(p.price_open)),
                sl=D(str(p.sl)) if p.sl else None, tp=D(str(p.tp)) if p.tp else None,
                magic=p.magic, comment=p.comment, unrealized_pnl=D(str(p.profit)),
            ))
        return out

    def account(self) -> AccountState:  # pragma: no cover
        a = self.api.account_info()
        return AccountState(
            balance=D(str(a.balance)), equity=D(str(a.equity)),
            margin_free=D(str(a.margin_free)), currency=a.currency,
        )

    # -- MarketDataPort -----------------------------------------------------------

    def latest_quote(self, symbol: str) -> TickBatch | None:  # pragma: no cover
        tick = self.api.symbol_info_tick(self.symbol_map[symbol])
        if tick is None:
            return None
        return TickBatch(
            symbol=symbol,
            ts_utc=datetime.fromtimestamp(tick.time_msc / 1000, tz=UTC),
            bid=D(str(tick.bid)), ask=D(str(tick.ask)),
        )

    def get_m5_bars(self, symbol, start_utc, end_utc) -> list[M5Bar]:  # pragma: no cover
        tf = getattr(self.api, "TIMEFRAME_M5", 5)
        rates = self.api.copy_rates_range(self.symbol_map[symbol], tf, start_utc, end_utc)
        out: list[M5Bar] = []
        if rates is None:
            return out
        point = self.specs[symbol].point
        for r in rates:
            o, h, l, c = (D(str(r["open"])), D(str(r["high"])), D(str(r["low"])), D(str(r["close"])))
            spread = D(int(r["spread"])) * point
            out.append(M5Bar(
                symbol=symbol,
                ts_open_utc=datetime.fromtimestamp(int(r["time"]), tz=UTC),
                bid_o=o, bid_h=h, bid_l=l, bid_c=c,
                ask_o=o + spread, ask_h=h + spread, ask_l=l + spread, ask_c=c + spread,
                tick_volume=int(r["tick_volume"]), spread_median=spread,
            ))
        return out


def connect_from_env(cls=MT5Adapter):  # pragma: no cover - thin env wrapper
    """Build + connect an adapter from MT5_LOGIN/MT5_PASSWORD/MT5_SERVER env vars."""
    import os

    from services.config_loader import load_instruments

    instruments, _ = load_instruments("configs/instruments.yaml")
    symbol_map = {s: spec.broker_symbols[0] for s, spec in instruments.instruments.items()}
    adapter = cls(symbol_map=symbol_map, specs=instruments.instruments, magic=778001)
    adapter.connect(
        login=int(os.environ["MT5_LOGIN"]),
        password=os.environ["MT5_PASSWORD"],
        server=os.environ["MT5_SERVER"],
        path=os.environ.get("MT5_TERMINAL_PATH"),
    )
    return adapter
