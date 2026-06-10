"""cTrader adapter — Phase 3 INTERFACE STUB ONLY (SPEC.md §10.3; build brief:
do not implement beyond a stub).

Planned shape: cTrader Open API (OAuth2 + Protobuf/FIX), true event callbacks,
Linux-friendly. Because the strategy/risk core talks only to ports, this is
adapter work only when Phase 3 arrives.
"""
from __future__ import annotations

from ports.execution import BracketOrder, OrderResult


class CTraderAdapter:
    """ExecutionPort/MarketDataPort stub. Constructing it is allowed (so wiring
    can be exercised); using it is not."""

    def __init__(self, *args, **kwargs):
        pass

    def place_bracket_market(self, order: BracketOrder) -> OrderResult:
        raise NotImplementedError("cTrader adapter is Phase 3 (SPEC §10.3)")

    def modify_position(self, ticket, sl, tp) -> OrderResult:
        raise NotImplementedError("cTrader adapter is Phase 3 (SPEC §10.3)")

    def close_position(self, ticket, lots=None) -> OrderResult:
        raise NotImplementedError("cTrader adapter is Phase 3 (SPEC §10.3)")

    def positions(self):
        raise NotImplementedError("cTrader adapter is Phase 3 (SPEC §10.3)")

    def account(self):
        raise NotImplementedError("cTrader adapter is Phase 3 (SPEC §10.3)")
