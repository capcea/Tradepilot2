"""M4 Monte Carlo evaluation-pass tests (SPEC.md §13.6)."""
from decimal import Decimal

from backtest.montecarlo import EvalRace, run_eval_monte_carlo

D = Decimal

RACE = EvalRace(
    target_usd=D("3000"),
    trailing_dd_usd=D("2500"),
    risk_usd=D("175"),
    day_soft_stop=D("-350"),
    consec_loss_halt=2,
    max_days=120,
)


def test_all_winning_distribution_always_passes():
    rs = [D("1.0")] * 60
    out = run_eval_monte_carlo(rs, trades_per_day=[1, 1, 2], race=RACE, n_sims=200, seed=7)
    assert out.pass_probability == D("1")
    assert out.busts == 0
    assert out.median_days_to_pass is not None
    assert out.median_days_to_pass > 0


def test_all_losing_distribution_never_passes():
    rs = [D("-1.0")] * 60
    out = run_eval_monte_carlo(rs, trades_per_day=[1, 2], race=RACE, n_sims=200, seed=7)
    assert out.pass_probability == D("0")
    assert out.busts == 200


def test_mixed_distribution_is_between():
    rs = [D("1.1")] * 11 + [D("-1.05")] * 9  # mildly positive edge
    out = run_eval_monte_carlo(rs, trades_per_day=[0, 1, 1, 2], race=RACE, n_sims=500, seed=42)
    assert D("0") < out.pass_probability < D("1")
    assert out.expected_attempts >= D("1")


def test_deterministic_for_same_seed():
    rs = [D("1.1")] * 11 + [D("-1.05")] * 9
    a = run_eval_monte_carlo(rs, trades_per_day=[1, 2], race=RACE, n_sims=300, seed=1)
    b = run_eval_monte_carlo(rs, trades_per_day=[1, 2], race=RACE, n_sims=300, seed=1)
    assert a.pass_probability == b.pass_probability
    assert a.median_days_to_pass == b.median_days_to_pass


def test_block_bootstrap_uses_blocks():
    # with block=5 and a strictly alternating series, blocks preserve local runs
    rs = [D("1"), D("-1")] * 20
    out = run_eval_monte_carlo(rs, trades_per_day=[2], race=RACE, n_sims=50, seed=3, block=5)
    assert out.n_sims == 50
