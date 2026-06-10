"""DXtrade adapter — Phase 3 INTERFACE STUB ONLY (SPEC.md §10.3; build brief:
do not implement beyond a stub).

Planned shape: REST + websocket, session-token auth, vendor-specific symbol
metadata. Adapter work only, thanks to the ports boundary.
"""
from __future__ import annotations

from ports.execution import BracketOrder, OrderResult


class DXtradeAdapter:
    def __init__(self, *args, **kwargs):
        pass

    def place_bracket_market(self, order: BracketOrder) -> OrderResult:
        raise NotImplementedError("DXtrade adapter is Phase 3 (SPEC §10.3)")

    def modify_position(self, ticket, sl, tp) -> OrderResult:
        raise NotImplementedError("DXtrade adapter is Phase 3 (SPEC §10.3)")

    def close_position(self, ticket, lots=None) -> OrderResult:
        raise NotImplementedError("DXtrade adapter is Phase 3 (SPEC §10.3)")

    def positions(self):
        raise NotImplementedError("DXtrade adapter is Phase 3 (SPEC §10.3)")

    def account(self):
        raise NotImplementedError("DXtrade adapter is Phase 3 (SPEC §10.3)")
