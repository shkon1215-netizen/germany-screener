"""Pre-flight diagnostic: tests every live call the Germany screen makes.

    python check_setup.py

Run this before main_de.py. Nearly every failure in this build is a SILENT
one - a missing market cap reads as "too small", a renamed symbol as "not
priced", a throttled Yahoo as a market where companies vanished - and a full
run is slow enough that finding out afterwards is expensive. Each check here
names what it protects against.
"""
from __future__ import annotations

import logging
import sys
import time

logging.basicConfig(level=logging.WARNING)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str):
    def deco(fn):
        def run():
            t0 = time.time()
            try:
                detail = fn() or ""
                RESULTS.append((name, True, detail))
                print(f"  OK    {name:<44} {detail}  ({time.time() - t0:.1f}s)")
            except Exception as e:                                # noqa: BLE001
                RESULTS.append((name, False, str(e)))
                print(f"  FAIL  {name:<44} {e}")
        return run
    return deco


@check("Deutsche Börse listed-companies workbook")
def roster():
    from config_de import ScreenConfig
    from providers_de import XetraProvider
    p = XetraProvider(ScreenConfig())
    r = p.listing_roster()
    if len(r) < 350:
        raise RuntimeError(f"only {len(r)} instruments - sheet layout changed?")
    for sym in ("SAP", "VOW3", "ALV", "DBK"):
        if sym not in set(r["symbol"]):
            raise RuntimeError(f"{sym} missing from the roster")
    if r["industry"].eq("").mean() > 0.05:
        raise RuntimeError("subsector column mostly empty - columns moved?")
    by = r["board"].value_counts().to_dict()
    return f"{len(r)} lines {by}, as at {p.roster_asof or '?'}"


@check("Yahoo .info on a Xetra line (rate limit)")
def info():
    import yfinance as yf
    i = yf.Ticker("SAP.DE").info
    if not i.get("marketCap") or not i.get("trailingPE"):
        raise RuntimeError("fields missing - Yahoo is throttling this IP; wait and retry")
    if i.get("currency") != "EUR":
        raise RuntimeError(f"currency {i.get('currency')!r}, expected EUR")
    return f"SAP mcap EUR {i['marketCap'] / 1e9:.0f}bn, P/E {i['trailingPE']:.1f}"


@check("market-cap fallback (fast_info)")
def fast():
    import yfinance as yf
    mc = yf.Ticker("ALV.DE").fast_info.get("marketCap")
    if not mc or mc < 5e10:
        raise RuntimeError(f"Allianz fast_info market cap {mc} - the fallback is broken, "
                           "and names Yahoo leaves blank will fail the size gate")
    return f"Allianz EUR {mc / 1e9:.0f}bn"


@check("dual-class market cap is all shares")
def dual():
    import yfinance as yf
    a = yf.Ticker("VOW.DE").info.get("marketCap")
    b = yf.Ticker("VOW3.DE").info.get("marketCap")
    if not a or not b:
        raise RuntimeError("no market cap on a VW line")
    if not 0.8 < a / b < 1.25:
        raise RuntimeError(f"VW St/Vz market caps differ {a / b:.2f}x - Yahoo stopped "
                           "pricing both classes; the size gate now sees one class only")
    return f"VW St {a / 1e9:.1f}bn vs Vz {b / 1e9:.1f}bn"


@check("ISIN -> Yahoo symbol resolution")
def isin():
    import yfinance as yf
    q = yf.Search("DE000SHA0100", max_results=8, news_count=0).quotes or []
    syms = [x.get("symbol") for x in q]
    if not any(str(s).startswith("SHA0") for s in syms):
        raise RuntimeError(f"Schaeffler's ISIN resolved to {syms} - renamed lines "
                           "will drop out as unpriced")
    return f"Schaeffler -> {syms}"


@check("batched price download (liquidity)")
def download():
    import yfinance as yf
    px = yf.download(["SAP.DE", "VOW3.DE", "KRN.DE"], period="30d", interval="1d",
                     progress=False, auto_adjust=False, group_by="column")
    if px.empty or "Volume" not in px.columns.get_level_values(0):
        raise RuntimeError("no Close/Volume panel")
    n = int(px["Volume"].notna().sum().min())
    if n < 10:
        raise RuntimeError(f"only {n} sessions of volume")
    return f"{n} sessions for 3 tickers"


@check("filed statements")
def statements():
    import yfinance as yf
    tk = yf.Ticker("SAP.DE")
    inc, bs = tk.income_stmt, tk.balance_sheet
    need_i = ("Total Revenue", "Net Income Common Stockholders")
    need_b = ("Common Stock Equity", "Ordinary Shares Number")
    miss = [k for k in need_i if k not in inc.index] + [k for k in need_b if k not in bs.index]
    if miss:
        raise RuntimeError(f"rows missing {miss} - Yahoo renamed statement lines")
    return f"{inc.shape[1]} columns, normalized income: {'Normalized Income' in inc.index}"


@check("FX EUR->USD")
def fx():
    from config_de import ScreenConfig
    from providers_de import XetraProvider
    r = XetraProvider(ScreenConfig()).eur_to_usd()
    if r == 1.15:
        raise RuntimeError("both live sources failed - hardcoded fallback in use")
    return f"1 EUR = {r:.4f} USD"


def main() -> int:
    print("Germany screener pre-flight\n")
    for fn in (roster, info, fast, dual, isin, download, statements, fx):
        fn()
    bad = [n for n, ok, _ in RESULTS if not ok]
    print("\n" + ("all checks passed - run python main_de.py -v"
                  if not bad else f"{len(bad)} check(s) failed: {', '.join(bad)}"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
