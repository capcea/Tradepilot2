"""M2 state-machine tests (SPEC.md §3.2)."""
import pytest

from core.state_machine import InvalidTransition, Phase, StateMachine


def test_happy_path_long_day():
    sm = StateMachine()
    assert sm.phase == Phase.IDLE
    for p in (Phase.RANGE_LOCKED, Phase.SWEPT_LOW, Phase.RECLAIMED,
              Phase.FILTERS_PASSED, Phase.ORDER_SENT, Phase.MANAGING, Phase.FLAT):
        sm = sm.to(p)
    assert sm.phase == Phase.FLAT


def test_flat_allows_next_setup_same_day():
    sm = StateMachine(Phase.FLAT)
    assert sm.to(Phase.SWEPT_HIGH).phase == Phase.SWEPT_HIGH


def test_swept_high_path():
    sm = StateMachine(Phase.RANGE_LOCKED).to(Phase.SWEPT_HIGH)
    assert sm.to(Phase.RECLAIMED).phase == Phase.RECLAIMED


def test_vetoed_setup_returns_to_watching():
    sm = StateMachine(Phase.RECLAIMED)
    assert sm.to(Phase.RANGE_LOCKED).phase == Phase.RANGE_LOCKED


@pytest.mark.parametrize("start", list(Phase))
def test_halt_reachable_from_every_phase(start):
    assert StateMachine(start).to(Phase.HALTED).phase == Phase.HALTED


def test_illegal_jump_raises():
    with pytest.raises(InvalidTransition):
        StateMachine(Phase.IDLE).to(Phase.MANAGING)
    with pytest.raises(InvalidTransition):
        StateMachine(Phase.SWEPT_LOW).to(Phase.ORDER_SENT)


def test_no_trade_is_terminal_except_halt():
    sm = StateMachine(Phase.RANGE_LOCKED).to(Phase.NO_TRADE)
    with pytest.raises(InvalidTransition):
        sm.to(Phase.SWEPT_LOW)
    assert sm.to(Phase.HALTED).phase == Phase.HALTED
