"""Diagnose Moomoo OpenD IV endpoints for a ticker (Phase 3 troubleshooting).

Run with OpenD up::

    make moomoo-iv-debug ticker=AAPL
    PYTHONPATH=. python scripts/moomoo_iv_debug.py --ticker AAPL

Prints the raw ``(ret, data)`` of every endpoint the earnings IV enrichment uses,
with the CORRECT call signatures and enum values, so you can see exactly which
call works and which one (and why) does not. Nothing here is swallowed.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from config.settings import settings  # noqa: E402

_BAR = "=" * 72


def _show(title: str, ret, data) -> None:
    import moomoo as ft  # type: ignore[import-untyped]

    ok = ret == ft.RET_OK
    print(f"\n{_BAR}\n{title}\n{_BAR}")
    print(f"ret = {ret}  ({'RET_OK' if ok else 'RET_ERROR'})")
    if not ok:
        print(f"error message = {data!r}")
        return
    if hasattr(data, "empty"):
        print(f"rows = {len(data)}  columns = {list(data.columns)}")
        with_opt = [c for c in data.columns if "implied" in c.lower() or "volatil" in c.lower()]
        if with_opt:
            print(f"IV/vol columns present: {with_opt}")
        print(data.head(8).to_string())
    else:
        print(repr(data)[:1500])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Moomoo IV endpoint diagnostics")
    parser.add_argument("--ticker", default="AAPL")
    args = parser.parse_args(argv)
    ticker = args.ticker.upper()
    code = f"US.{ticker}"

    try:
        import moomoo as ft  # type: ignore[import-untyped]
        from moomoo.common.pb.Qot_Common_pb2 import (  # type: ignore[import-untyped]
            OptionVolatilityTimePeriodType_Year,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"moomoo SDK import failed: {exc}", file=sys.stderr)
        return 1

    ctx = ft.OpenQuoteContext(host=settings.MOOMOO_HOST, port=settings.MOOMOO_PORT)
    try:
        # 1. Stock snapshot (spot price; has no IV/earnings for a stock code).
        _show("1) get_market_snapshot([stock])", *ctx.get_market_snapshot([code]))

        # 2. Option volatility — the IV-rank source. CORRECT period enum (Year=5).
        print(f"\n[note] OptionVolatilityTimePeriodType_Year = {OptionVolatilityTimePeriodType_Year} "
              f"(NOT ft.RangePeriod.ONE_YEAR={int(ft.RangePeriod.ONE_YEAR)}, which is Quarter here)")
        _show(
            "2) get_option_volatility(code, queryTimePeriod=Year, hvTimePeriod=HV_365D)",
            *ctx.get_option_volatility(
                code,
                query_time_period=OptionVolatilityTimePeriodType_Year,
                hv_time_period=int(ft.OptionHVPeriod.HV_365D),
            ),
        )
        # 2b. Same call with server-default period (in case the explicit one is rejected).
        _show(
            "2b) get_option_volatility(code) [server-default period]",
            *ctx.get_option_volatility(code),
        )

        # 3. Option expiries.
        ret_e, exp = ctx.get_option_expiration_date(code)
        _show("3) get_option_expiration_date(code)", ret_e, exp)

        # 4. Option chain for the nearest expiry + ATM-option IV via snapshot.
        if ret_e == ft.RET_OK and hasattr(exp, "empty") and not exp.empty:
            expiry = str(sorted(exp["strike_time"])[0])
            ret_c, chain = ctx.get_option_chain(code, start=expiry, end=expiry)
            _show(f"4) get_option_chain(code, start=end={expiry})", ret_c, chain)
            if ret_c == ft.RET_OK and hasattr(chain, "empty") and not chain.empty:
                opt_codes = list(chain["code"].head(2))
                _show(
                    f"4b) get_market_snapshot(option codes {opt_codes}) — has option_implied_volatility",
                    *ctx.get_market_snapshot(opt_codes),
                )

        # 5. Basic info (sanity).
        _show(
            "5) get_stock_basicinfo(US, STOCK, [code])",
            *ctx.get_stock_basicinfo(
                market=ft.Market.US, stock_type=ft.SecurityType.STOCK, code_list=[code]
            ),
        )
    finally:
        ctx.close()

    print(f"\n{_BAR}\nDone. Endpoint (2) drives IV rank; (4b) is the ATM-IV fallback.\n{_BAR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
