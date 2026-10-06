"""Offline validation of the Germany screener. No network required.

Run: python test_germany.py

Every planted case is something a naive screener gets wrong. The dual-class
lines are the important ones - they are Germany's counterpart of Korea's
preferred shares, and they are planted both ways round, because in Frankfurt
the cheap line is sometimes the one to keep and sometimes the one to drop.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import config_de as K
import de_filters as DF
from config_de import ScreenConfig
from providers_de import to_yahoo
from screener import run_screen

rng = np.random.default_rng(7)

USD_PER_EUR = 1.12
MCAP = 9.0e8           # ~USD 1.0bn, clears the gate
ADV = 6.0e6            # ~USD 6.7m, clears the gate


def base(**kw):
    r = dict(name="", symbol="", isin="", board="PRIME", tier="SDAX", tecdax=False,
             sector="Industrial", industry="Industrial Machinery",
             country="Germany", venue="XETRA + FRANKFURT", quote_type="EQUITY",
             market_cap_local=MCAP, adv_local=ADV, close_local=50.0, currency="EUR",
             trailing_pe=np.nan, price_to_book=np.nan, ev_to_ebitda=np.nan,
             trailing_eps=4.0, book_value_ps=32.0, div_yield=1.5, yf_industry="")
    r.update(kw)
    r["ticker"] = to_yahoo(r["symbol"], r["venue"])
    if not r["isin"]:
        r["isin"] = "DE000" + (r["symbol"] + "XXXXXX")[:6] + "0"
    return r


def make_universe() -> pd.DataFrame:
    rows = []
    # Two exchange subsectors with different multiple levels.
    specs = [("Industrial Machinery", "Industrial", 15.0, 1.8, 9.0, 18),
             ("Software", "Software", 32.0, 5.0, 20.0, 14)]
    n = 0
    for ind, sec, pe, pb, ev, count in specs:
        for _ in range(count):
            n += 1
            rows.append(base(symbol=f"P{n:02d}", name=f"PEER {n} AG",
                             industry=ind, sector=sec,
                             trailing_pe=pe * rng.uniform(0.93, 1.09),
                             price_to_book=pb * rng.uniform(0.93, 1.09),
                             ev_to_ebitda=ev * rng.uniform(0.93, 1.09),
                             market_cap_local=MCAP * rng.uniform(0.9, 2.5),
                             adv_local=ADV * rng.uniform(0.9, 2.5)))

    # --- planted cases ------------------------------------------------
    # PASS: genuinely cheap vs machinery peers, good ROE, pays a dividend
    rows.append(base(symbol="GOOD", name="GENUINE VALUE AG",
                     trailing_pe=8.5, price_to_book=0.75, ev_to_ebitda=5.0,
                     trailing_eps=5.0, book_value_ps=40.0, div_yield=4.2))

    # FAIL: listed PE vehicle - cheap on everything, excluded by subsector.
    rows.append(base(symbol="DBAN", name="DEUTSCHE BETEILIGUNGS AG",
                     sector="Financial Services",
                     industry="Private Equity & Venture Capital",
                     trailing_pe=5.0, price_to_book=0.7, ev_to_ebitda=3.0,
                     trailing_eps=4.0, book_value_ps=40.0, div_yield=3.0))

    # FAIL: listed landlord. P/E of 3.5 from an IAS 40 revaluation gain.
    rows.append(base(symbol="LEG", name="LEG IMMOBILIEN SE", sector="Financial Services",
                     industry="Real Estate", trailing_pe=3.5, price_to_book=0.4,
                     ev_to_ebitda=18.0, trailing_eps=14.0, book_value_ps=110.0, div_yield=6.5))

    # FAIL: REIT by name.
    rows.append(base(symbol="HABA", name="HAMBORNER REIT AG", industry="Industrial Machinery",
                     trailing_pe=9.0, price_to_book=0.8, ev_to_ebitda=6.0))

    # FAIL: foreign issuer with no German index membership - a secondary line.
    rows.append(base(symbol="CRIN", name="UNICREDIT", country="Italy", tier="",
                     trailing_pe=7.0, price_to_book=0.9, ev_to_ebitda=5.0,
                     trailing_eps=6.0, book_value_ps=45.0, div_yield=5.0))
    # KEEP: foreign issuer that IS in a German index (the Airbus/Qiagen shape).
    rows.append(base(symbol="QIA", name="QIAGEN N.V.", country="Netherlands", tier="DAX",
                     industry="Software", sector="Software",
                     trailing_pe=31.0, price_to_book=5.0, ev_to_ebitda=19.0))

    # Dual class, Volkswagen shape: the Stamm is flagged as the index line but
    # barely trades; the Vorzug is the liquid one. Keep VOW3, drop VOW.
    rows.append(base(symbol="VOW", isin="DE0007664005", name="VOLKSWAGEN AG ST O.N.",
                     tier="DAX", adv_local=0.4e6, trailing_pe=14.0, price_to_book=1.7,
                     ev_to_ebitda=8.8))
    rows.append(base(symbol="VOW3", isin="DE0007664039", name="VOLKSWAGEN AG VZO O.N.",
                     tier="", adv_local=60e6, trailing_pe=14.2, price_to_book=1.72,
                     ev_to_ebitda=8.9))
    # Dual class, Sixt shape: the Stamm is liquid, the Vorzug sits ~20% below
    # it and would pass the screen on that gap alone. Keep SIX2, drop SIX3.
    rows.append(base(symbol="SIX2", isin="DE0007231326", name="SIXT SE ST O.N.",
                     adv_local=8e6, trailing_pe=14.5, price_to_book=1.75, ev_to_ebitda=8.7))
    rows.append(base(symbol="SIX3", isin="DE0007231334", name="SIXT SE VZO O.N.",
                     tier="", adv_local=1e6, trailing_pe=8.0, price_to_book=0.9,
                     ev_to_ebitda=5.5, trailing_eps=5.0, book_value_ps=40.0, div_yield=5.0))
    # Vorzug as the only listed line (Porsche SE / Jungheinrich shape): kept, flagged.
    rows.append(base(symbol="JUN3", isin="DE0006219934", name="JUNGHEINRICH AG O.N.VZO",
                     trailing_pe=15.5, price_to_book=1.8, ev_to_ebitda=9.2))
    # Two DIFFERENT companies whose ISINs share nine characters (ATOSS and
    # SYZYGY really do). The name must keep them apart.
    rows.append(base(symbol="AOF", isin="DE0005104400", name="ATOSS SOFTWARE SE",
                     industry="Software", sector="Software",
                     trailing_pe=33.0, price_to_book=5.2, ev_to_ebitda=21.0))
    rows.append(base(symbol="SYZ", isin="DE0005104806", name="SYZYGY AG O.N.",
                     industry="Software", sector="Software",
                     trailing_pe=30.0, price_to_book=4.8, ev_to_ebitda=19.0))

    # FAIL (ROE): cheap holdco with 2% ROE - arithmetic, not a discount.
    rows.append(base(symbol="HOLD", name="FAMILIEN HOLDING AG",
                     trailing_pe=9.0, price_to_book=0.55, ev_to_ebitda=5.5,
                     trailing_eps=0.8, book_value_ps=40.0, div_yield=2.5))

    # FAIL: loss-maker. Yahoo sometimes reports P/E 0; it must read as missing.
    rows.append(base(symbol="LOSS", name="VERLUST AG", trailing_pe=0.0,
                     price_to_book=0.6, ev_to_ebitda=np.nan,
                     trailing_eps=-2.0, book_value_ps=30.0))

    # A bank: EV/EBITDA must be suppressed even though Yahoo gives one, and it
    # clears the absolute screen via the P/B + ROE carve-out.
    rows.append(base(symbol="DBK", name="DEUTSCHE BANK AG NA O.N.", sector="Banks",
                     industry="Credit Banks", trailing_pe=9.4, price_to_book=0.74,
                     ev_to_ebitda=4.0, trailing_eps=3.3, book_value_ps=42.0, div_yield=3.2))

    # Thin subsector: one member alone. Must fall back to the exchange SECTOR
    # (Industrial) rather than go unscored.
    rows.append(base(symbol="LONE", name="EINZELGAENGER AG", industry="Heavy Machinery",
                     trailing_pe=16.0, price_to_book=1.9, ev_to_ebitda=9.5))

    # FAIL (size): too small.
    rows.append(base(symbol="TINY", name="KLEIN AG", market_cap_local=2e8,
                     trailing_pe=6.0, price_to_book=0.5, ev_to_ebitda=3.0))
    return pd.DataFrame(rows)


def check_helpers() -> list[str]:
    bad = []
    print("=== helpers ===")

    def ok(cond, label):
        print(f"  {'OK  ' if cond else 'FAIL'} {label}")
        if not cond:
            bad.append(label)

    ok(to_yahoo("VOW3") == "VOW3.DE", "Xetra symbol -> .DE")
    ok(to_yahoo("LNSX", "FRANKFURT") == "LNSX.F", "Frankfurt-only line -> .F (no .DE quote exists)")
    ok(K.norm_subsector("Pharmatceuticals") == "Pharmaceuticals"
       and K.norm_subsector("HealthCare") == "Health Care"
       and K.norm_subsector("Retail,Internet") == "Retail, Internet",
       "exchange subsector typos folded into one cohort each")
    ok(K.norm_sector("Financial services") == "Financial Services"
       and K.norm_sector("Industrial ") == "Industrial"
       and K.norm_sector("Basic resources") == "Basic Resources"
       and K.norm_sector("-") == "",
       "exchange sector spellings normalised")
    ok(K.is_financial("Banks") and K.is_financial("Insurance")
       and K.is_financial("Financial Services", "Diversified Financial"),
       "banks, insurers, diversified financials are financials")
    ok(not K.is_financial("Financial Services", "Real Estate"),
       "real estate is NOT a financial - its EV/EBITDA is meaningful")
    ok(K.is_pref_name("VOLKSWAGEN AG VZO O.N.") and K.is_pref_name("Porsche AG Vz")
       and K.is_pref_name("MINERALBR.UEBERK.-VZ-") and not K.is_pref_name("VERBIO SE ON"),
       "Vorzug detected from every spelling the exchange uses")
    ok(K.issuer_key("DE0006048408", "HENKEL AG+CO.KGAA ST O.N.")
       == K.issuer_key("DE0006048432", "HENKEL AG+CO.KGAA VZO"),
       "Stamm and Vorzug of Henkel group as one issuer")
    ok(K.issuer_key("DE0005550602", "DRAEGERWERK ST.A.O.N.")
       == K.issuer_key("DE0005550636", "DRAEGERWERK VZO O.N."),
       "Drägerwerk's two lines group as one issuer despite 'ST.A.O.N.'")
    ok(K.issuer_key("DE0005104400", "ATOSS SOFTWARE SE")
       != K.issuer_key("DE0005104806", "SYZYGY AG O.N."),
       "ATOSS and SYZYGY share an ISIN prefix but are different issuers")
    return bad


def check_statements() -> list[str]:
    from providers_de import build_statement_record, cagr
    bad = []
    print("\n=== filed statements ===")
    years = pd.to_datetime(["2021-12-31", "2022-12-31", "2023-12-31",
                            "2024-12-31", "2025-12-31"])

    def stmt(rows: dict) -> pd.DataFrame:
        # Yahoo shape: line items as rows, newest year first, oldest column empty.
        df = pd.DataFrame({k: [np.nan] + list(v) for k, v in rows.items()}, index=years).T
        return df[df.columns[::-1]]

    def company(ni=(100e6, 110e6, 120e6, 130e6), shares=(1e8,) * 4):
        inc = stmt({"Total Revenue": (1000e6, 1100e6, 1200e6, 1300e6),
                    "Operating Income": (150e6, 160e6, 170e6, 180e6),
                    "EBITDA": (200e6, 210e6, 220e6, 230e6),
                    "Net Income Common Stockholders": ni})
        bs = stmt({"Common Stock Equity": (1000e6,) * 4, "Ordinary Shares Number": shares,
                   "Total Debt": (300e6,) * 4, "Cash And Cash Equivalents": (100e6,) * 4})
        return inc, bs

    px = pd.Series(20.0, index=pd.date_range("2020-01-01", "2026-10-02", freq="B"))

    def ok(cond, label):
        print(f"  {'OK  ' if cond else 'FAIL'} {label}")
        if not cond:
            bad.append(label)

    # A euro reporter at EUR 20, 100m shares: EUR 2bn against EUR 1bn equity.
    inc, bs = company()
    rec = build_statement_record(inc, bs, px, None, "EUR", "EUR", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_years"] == "2022,2023,2024,2025", "Yahoo's empty padded year is not a filed year")
    ok(rec["hist_pbr"] == [2.0] * 4, f"P/B history in euros, no subunit ({rec['hist_pbr']})")
    ok(rec["fin_years"] == "2023,2024,2025" and rec["rev_y3"] == 1300.0,
       "3-year history is the last three filed years, in millions")
    ok(np.isnan(cagr([-50, 10, 20])), "CAGR undefined on a negative base")

    # A DOLLAR reporter quoted in euros - the Qiagen shape. EUR 2bn at 1.10
    # USD/EUR is USD 2.2bn over USD 1bn of equity: P/B 2.2.
    fx = pd.Series(1.10, index=px.index)
    rec = build_statement_record(inc, bs, px, fx, "EUR", "USD", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_pbr"] == [2.2] * 4, f"USD reporter converted at each year-end ({rec['hist_pbr']})")
    rec = build_statement_record(inc, bs, px, None, "EUR", "USD", close_now=20.0, mcap_now=2e9)
    ok(all(v is None for v in rec["hist_pbr"]), "USD reporter without FX -> no benchmark")

    # Dual-class market cap: Yahoo prices ALL shares (both classes) at this
    # line's price, and the filed count is both classes too - so the basis
    # check must pass. 100m filed shares x EUR 20 = EUR 2bn = market cap.
    rec = build_statement_record(inc, bs, px, None, "EUR", "EUR", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_note"] == "", "dual-class: all-shares market cap matches filed shares")

    # Stock split 1:10 between filings (Einhell's 2024 shape) -> no history.
    inc2, bs2 = company(shares=(1e7, 1e7, 1e8, 1e8))
    rec = build_statement_record(inc2, bs2, px, None, "EUR", "EUR", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_note"] == "share-count break" and rec["hist_pbr"] == [],
       "share split -> no history, not a 90% 'premium'")

    # One-off gain in the latest year: normalized earnings carry the valuation.
    inc4, bs4 = company(ni=(100e6, 100e6, 100e6, 200e6))
    inc4.loc["Normalized Income"] = inc4.loc["Net Income Common Stockholders"]
    inc4.loc["Normalized Income", years[-1]] = 100e6
    inc4.loc["Normalized EBITDA"] = inc4.loc["EBITDA"]
    rec = build_statement_record(inc4, bs4, px, None, "EUR", "EUR", close_now=20.0, mcap_now=2e9)
    ok(rec["hist_earnings"] == "normalized" and rec["hist_per"][-1] == 20.0,
       "one-off gain: valuation on normalized earnings")
    return bad


def check_split_basis() -> list[str]:
    """A split or consolidation Yahoo has priced in but not yet restated.

    Johnson Matthey (Aug 2026) is the shape: a 4-for-3 consolidation recorded
    as 0.75, prices rescaled at once, the filing still on the old share count.
    """
    from providers_de import build_statement_record
    import de_filters as F
    bad = []
    print("\n=== splits since the latest filing ===")

    def ok(cond, label):
        print(f"  {'OK  ' if cond else 'FAIL'} {label}")
        if not cond:
            bad.append(label)

    years = pd.to_datetime(["2021-12-31", "2022-12-31", "2023-12-31",
                            "2024-12-31", "2025-12-31"])

    def stmt(rows):
        df = pd.DataFrame({k: [np.nan] + list(v) for k, v in rows.items()}, index=years).T
        return df[df.columns[::-1]]

    def company(shares):
        inc = stmt({"Total Revenue": (1000e6,) * 4, "Operating Income": (150e6,) * 4,
                     "EBITDA": (200e6,) * 4,
                     "Net Income Common Stockholders": (100e6,) * 4})
        bs = stmt({"Common Stock Equity": (1000e6,) * 4, "Ordinary Shares Number": shares,
                    "Total Debt": (300e6,) * 4, "Cash And Cash Equivalents": (100e6,) * 4})
        return inc, bs

    idx = pd.date_range("2020-01-01", "2026-10-02", freq="B")
    # Price series as Yahoo serves it: adjusted for every split it recorded.
    px = pd.Series(20.0, index=idx)
    split_day = pd.Timestamp("2026-08-03")

    def splits(ratio):
        return pd.Series([ratio], index=[split_day])

    # 1. Consolidation 4-for-3, filing NOT restated: 100m filed shares, 75m
    #    today. Market cap = price x 75m. True P/B is 0.75 x the naive one.
    inc, bs = company((100e6,) * 4)
    rec = build_statement_record(inc, bs, px, None, "EUR", "EUR",
                                 close_now=20.0, mcap_now=20.0 * 75e6,
                                 splits=splits(0.75))
    ok(rec["hist_note"] == "" and abs(rec["share_basis_g"] - 0.75) < 1e-9
       and rec["hist_pbr"] == [1.5] * 4,
       f"unrestated consolidation: history restated by 0.75 ({rec['hist_pbr']})")

    # 2. Split 5-for-1, filing NOT restated: 20m filed, 100m today.
    inc, bs = company((20e6,) * 4)
    rec = build_statement_record(inc, bs, px, None, "EUR", "EUR",
                                 close_now=20.0, mcap_now=20.0 * 100e6,
                                 splits=splits(5.0))
    ok(abs(rec["share_basis_g"] - 5.0) < 1e-9 and rec["hist_pbr"] == [2.0] * 4,
       f"unrestated split: history restated by 5 ({rec['hist_pbr']})")

    # 3. Split recorded AND restated: filed shares already 100m -> leave alone.
    inc, bs = company((100e6,) * 4)
    rec = build_statement_record(inc, bs, px, None, "EUR", "EUR",
                                 close_now=20.0, mcap_now=20.0 * 100e6,
                                 splits=splits(5.0))
    ok(rec["share_basis_g"] == 1.0 and rec["hist_pbr"] == [2.0] * 4,
       "split already restated by Yahoo: left alone")

    # 4. Share count off 5x and nothing recorded to explain it: refuse.
    inc, bs = company((20e6,) * 4)
    rec = build_statement_record(inc, bs, px, None, "EUR", "EUR",
                                 close_now=20.0, mcap_now=20.0 * 100e6, splits=None)
    ok(rec["hist_note"] == "price/share basis mismatch" and rec["hist_pbr"] == [],
       "unexplained share-count gap refused, not guessed")

    # 5. .info per-share fields. Filed equity 1000m over 100m old shares; g=0.75.
    df = pd.DataFrame([
        # still on the OLD basis: bookValue = 1000m / 100m = 10
        dict(share_basis_g=0.75, lf_equity=1000e6, lf_shares=100e6, book_value_ps=10.0,
             trailing_eps=1.0, trailing_pe=20.0, price_to_book=2.0, div_yield=4.0),
        # already on the NEW basis: bookValue = 1000m / 75m
        dict(share_basis_g=0.75, lf_equity=1000e6, lf_shares=100e6, book_value_ps=1000e6 / 75e6,
             trailing_eps=4 / 3, trailing_pe=15.0, price_to_book=1.5, div_yield=4.0),
        # neither
        dict(share_basis_g=0.75, lf_equity=1000e6, lf_shares=100e6, book_value_ps=30.0,
             trailing_eps=1.0, trailing_pe=20.0, price_to_book=2.0, div_yield=4.0),
        # no split: untouched
        dict(share_basis_g=1.0, lf_equity=1000e6, lf_shares=100e6, book_value_ps=10.0,
             trailing_eps=1.0, trailing_pe=20.0, price_to_book=2.0, div_yield=4.0),
    ])
    out, _ = F.restate_info_for_splits(df)
    ok(abs(out.loc[0, "price_to_book"] - 1.5) < 1e-9 and abs(out.loc[0, "div_yield"] - 4 / 0.75) < 1e-9
       and out.loc[0, "split_note"] == "info restated for split",
       ".info on the old basis: P/B and yield restated")
    ok(out.loc[1, "price_to_book"] == 1.5 and np.isnan(out.loc[1, "div_yield"]),
       ".info on the new basis: per-share kept, unverifiable yield missing")
    ok(np.isnan(out.loc[2, "price_to_book"]) and out.loc[2, "split_note"] == "info basis unclear",
       ".info on neither basis: refused")
    ok(out.loc[3, "price_to_book"] == 2.0 and out.loc[3, "div_yield"] == 4.0
       and out.loc[3, "split_note"] == "",
       "no split since the filing: untouched")
    return bad


def check_history_screen() -> list[str]:
    bad = []
    print("\n=== own-history screen ===")
    cfg = ScreenConfig()

    def row(sym, pers, pbrs, evxs, now_mcap, sector="Industrial", industry="Industrial Machinery",
            roe_ok=True):
        return dict(symbol=sym, ticker=sym + ".DE", sector=sector, industry=industry,
                    hist_per=pers, hist_pbr=pbrs, hist_evx=evxs,
                    market_cap_local=now_mcap, fx_now=1.0, lf_ni=100e6,
                    lf_equity=1000e6, lf_ebitda=200e6, lf_debt=300e6,
                    lf_cash=100e6, lf_mi=0.0, passes=False, abs_passes=False,
                    roe_ok=roe_ok, avg_discount=0.0, hist_note="")

    df = pd.DataFrame([
        row("DRTD", [20, 21, 19, 20], [2.0, 2.1, 1.9, 2.0], [10, 11, 9, 10], 1.0e9),
        row("LOWR", [20, 21, 19, 20], [2.0, 2.1, 1.9, 2.0], [10, 11, 9, 10], 1.0e9, roe_ok=False),
        row("THIN", [20, None, None, 21], [2.0, None, None, 2.1], [10, None, None, 11], 1.0e9),
        row("BANK", [20, 21, 19, 20], [2.0, 2.1, 1.9, 2.0], [10, 11, 9, 10], 1.0e9,
            sector="Banks", industry="Credit Banks"),
        row("PROP", [20, 21, 19, 20], [2.0, 2.1, 1.9, 2.0], [10, 11, 9, 10], 1.0e9,
            sector="Financial Services", industry="Real Estate"),
    ])
    res, _ = DF.apply_history_screen(df, cfg)
    idx = res.set_index("symbol")

    def ok(cond, label):
        print(f"  {'OK  ' if cond else 'FAIL'} {label}")
        if not cond:
            bad.append(label)

    d = idx.loc["DRTD"]
    ok(bool(d["hist_passes"]) and d["screen"] == "history", "de-rated name passes on history alone")
    ok(abs(d["per_now"] - 10.0) < 1e-9, "today measured on the history's basis")
    ok(not bool(idx.loc["LOWR", "hist_passes"]), "ROE floor applies to the history screen")
    ok(idx.loc["THIN", "hist_n_valid"] == 0, "fewer than 3 usable years -> no benchmark")
    ok(pd.isna(idx.loc["BANK", "hist_evx_med"]) and pd.isna(idx.loc["BANK", "evx_now"]),
       "banks: EV/EBITDA skipped in history and today (exchange sector)")
    ok(pd.notna(idx.loc["PROP", "evx_now"]), "real estate keeps EV/EBITDA (not a financial)")
    return bad


def main() -> int:
    failures = check_helpers()
    cfg = ScreenConfig()
    df = make_universe()

    df, gstats = DF.apply_german_filters(df, cfg)
    df["market_cap_usd"] = df["market_cap_local"] * USD_PER_EUR
    df = df[df["market_cap_usd"] >= cfg.min_market_cap_usd]
    df, lstats = DF.keep_one_line_per_issuer(df)
    res, stats = run_screen(df, USD_PER_EUR, cfg)
    res = DF.add_quality_context(res)
    res = DF.add_valueup_flags(res)
    res, _ = DF.apply_roe_gate(res, cfg)
    res, _ = DF.apply_absolute_screen(res, cfg)
    idx = res.set_index("symbol")

    print("\n=== universe hygiene ===")
    print(f"  funnel: {gstats} {lstats}")

    def ok(cond, label):
        print(f"  {'OK  ' if cond else 'FAIL'} {label}")
        if not cond:
            failures.append(label)

    for sym, why in [("DBAN", "listed PE vehicle excluded by exchange subsector"),
                     ("LEG", "listed landlord excluded (IAS 40 P/E)"),
                     ("HABA", "REIT excluded"),
                     ("CRIN", "foreign secondary line excluded"),
                     ("VOW", "illiquid Stamm dropped although it is the index line"),
                     ("SIX3", "cheap but illiquid Vorzug dropped - would otherwise pass"),
                     ("TINY", "below the size floor")]:
        ok(sym not in idx.index, why)
    ok("QIA" in idx.index, "foreign issuer IN a German index kept")
    ok("VOW3" in idx.index and bool(idx.loc["VOW3", "is_pref"]),
       "liquid Vorzug kept for VW, tagged as Vorzug")
    ok("SIX2" in idx.index, "liquid Stamm kept for Sixt")
    ok("JUN3" in idx.index and bool(idx.loc["JUN3", "is_pref_only"]),
       "Vorzug that is the only listed line kept and flagged")
    ok("AOF" in idx.index and "SYZ" in idx.index,
       "two companies sharing an ISIN prefix are not merged")

    print("\n=== screens ===")
    ok(bool(idx.loc["GOOD", "passes"]), "genuinely cheap name passes the peer screen")
    ok(not bool(idx.loc["HOLD", "passes"]) and idx.loc["HOLD", "n_metrics_passing"] >= 2,
       "cheap holdco with 2% ROE fails on ROE, not on cheapness")
    ok(pd.isna(idx.loc["LOSS", "trailing_pe"]), "P/E of 0 read as missing")
    ok(pd.isna(idx.loc["DBK", "ev_to_ebitda"]), "bank's EV/EBITDA suppressed (exchange sector)")
    ok(bool(idx.loc["DBK", "abs_passes"]) and bool(idx.loc["DBK", "abs_via_carveout"]),
       "bank clears the absolute screen via the P/B + ROE carve-out")
    ok(idx.loc["LONE", "trailing_pe_peer_basis"] == "sector",
       f"thin subsector falls back to the exchange sector "
       f"({idx.loc['LONE', 'trailing_pe_peer_basis']!r})")
    ok(idx.loc["GOOD", "trailing_pe_peer_basis"] == "industry",
       "a populated subsector benchmarks within itself")

    failures += check_statements()
    failures += check_history_screen()
    failures += check_split_basis()

    print("\n=== passing ===")
    hits = res[res["passes_any"]][["symbol", "name", "industry", "trailing_pe",
                                   "price_to_book", "roe_pct", "screen"]]
    print(hits.round(2).to_string(index=False) if not hits.empty else "  (none)")
    print("\n" + ("ALL CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
