"""Quick data-coverage stats for the market DB (M1/M4 validation aid)."""
import sys

import sqlalchemy as sa


def main() -> None:
    db = sys.argv[1] if len(sys.argv) > 1 else "data/market.sqlite"
    engine = sa.create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            "SELECT symbol, tf, COUNT(*) AS n, MIN(ts_utc) AS lo, MAX(ts_utc) AS hi "
            "FROM candle GROUP BY symbol, tf"
        )).fetchall()
    for r in rows:
        print(f"{r.symbol} {r.tf}: {r.n} bars  {r.lo} .. {r.hi}")


if __name__ == "__main__":
    main()
