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

    from datetime import date

    ctx = ft.OpenQuoteContext(host=settings.MOOMOO_HOST, port=settings.MOOMOO_PORT)
    try:
        # 1. Stock snapshot → spot price (a stock code carries NO IV/earnings).
        ret_s, snap = ctx.get_market_snapshot([code])
        _show("1) get_market_snapshot([stock]) → spot price", ret_s, snap)
        spot = None
        if ret_s == ft.RET_OK and hasattr(snap, "empty") and not snap.empty:
            spot = float(snap.iloc[0].get("last_price") or 0) or None
        print(f"\n[spot] {ticker} last_price = {spot}")

        # 2. Option volatility on the STOCK code — expected to FAIL ("only option
        #    codes are supported"). This is why the old code returned no IV.
        _show(
            "2) get_option_volatility(STOCK code) — expected RET_ERROR",
            *ctx.get_option_volatility(
                code, query_time_period=OptionVolatilityTimePeriodType_Year,
                hv_time_period=int(ft.OptionHVPeriod.HV_365D),
            ),
        )

        # 3. Option expiries → pick the nearest LIVE one (>= today), never expired.
        ret_e, exp = ctx.get_option_expiration_date(code)
        _show("3) get_option_expiration_date(code)", ret_e, exp)
        live_expiry = None
        if ret_e == ft.RET_OK and hasattr(exp, "empty") and not exp.empty:
            today = date.today().isoformat()
            live = sorted(str(s) for s in exp["strike_time"] if str(s) >= today)
            live_expiry = live[0] if live else None
        print(f"\n[expiry] nearest LIVE expiry (>= today) = {live_expiry}")

        # 4. Chain for the live expiry → find the ATM option code (closest to spot).
        atm_call = atm_put = None
        if live_expiry:
            ret_c, chain = ctx.get_option_chain(code, start=live_expiry, end=live_expiry)
            _show(f"4) get_option_chain(code, start=end={live_expiry})", ret_c, chain)
            if ret_c == ft.RET_OK and hasattr(chain, "empty") and not chain.empty and spot:
                rows = chain.to_dict("records")
                calls = [r for r in rows if str(r.get("option_type", "")).upper().endswith("CALL")]
                puts = [r for r in rows if str(r.get("option_type", "")).upper().endswith("PUT")]
                if calls:
                    atm_call = min(calls, key=lambda r: abs(float(r["strike_price"]) - spot))["code"]
                if puts:
                    atm_put = min(puts, key=lambda r: abs(float(r["strike_price"]) - spot))["code"]
        print(f"\n[ATM] call={atm_call}  put={atm_put}  (spot={spot})")

        # 4b. Snapshot the ATM options → option_implied_volatility (the WORKING IV).
        if atm_call and atm_put:
            _show(
                "4b) get_market_snapshot([ATM call, ATM put]) — option_implied_volatility",
                *ctx.get_market_snapshot([atm_call, atm_put]),
            )
            # 5. Option-vol on the ATM OPTION code → IV rank series (correct usage).
            _show(
                "5) get_option_volatility(ATM OPTION code, Year) — IV rank series",
                *ctx.get_option_volatility(
                    atm_call, query_time_period=OptionVolatilityTimePeriodType_Year,
                    hv_time_period=int(ft.OptionHVPeriod.HV_365D),
                ),
            )
    finally:
        ctx.close()

    print(f"\n{_BAR}\nWorking IV path: (4b) ATM option snapshot = current IV + straddle "
          f"expected move; (5) on the ATM OPTION code = IV rank.\n"
          f"(2) on the STOCK code is EXPECTED to fail.\n{_BAR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
