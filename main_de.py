"""Germany (Deutsche Börse) relative-valuation screener.

  python main_de.py -v                       # full run, every segment
  python main_de.py --board PRIME            # Prime Standard only
  python main_de.py --skip-liquidity         # size alone gates (see README)
  python main_de.py --include-investment-cos # see why this is off by default
  python main_de.py --discount 0.15 --min-metrics 1
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

import pandas as pd

import de_filters as DF
from config_de import BOARDS, ScreenConfig
from providers_de import XetraProvider
from screener import run_screen

D = ScreenConfig()      # argparse defaults come from here, so the two cannot drift


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="de_screen_results.csv")
    p.add_argument("--board", choices=[*BOARDS, "ALL"], default="ALL",
                   help="exchange segment; ALL screens them together (default)")
    p.add_argument("--min-mcap", type=float, default=D.min_market_cap_usd, help="USD")
    p.add_argument("--min-adv", type=float, default=D.min_adv_usd,
                   help="USD median daily traded value on Xetra")
    p.add_argument("--discount", type=float, default=D.discount_threshold)
    p.add_argument("--min-metrics", type=int, default=D.min_metrics_passing)
    p.add_argument("--min-peers", type=int, default=D.min_peers)
    p.add_argument("--peer-keys", default=",".join(D.peer_keys))
    p.add_argument("--fallback-peer-keys", default=",".join(D.fallback_peer_keys))
    p.add_argument("--fx", type=float, help="USD per EUR (default: live)")
    p.add_argument("--include-investment-cos", action="store_true",
                   help="keep listed private-equity / holding vehicles; they trade "
                        "at a standing discount to NAV")
    p.add_argument("--include-reits", action="store_true")
    p.add_argument("--include-property", action="store_true",
                   help="keep listed landlords (Vonovia, LEG...); their P/E carries "
                        "IAS 40 revaluation gains")
    p.add_argument("--both-lines", action="store_true",
                   help="keep Stamm AND Vorzug of dual-class issuers (double-counts)")
    p.add_argument("--exclude-holdcos", action="store_true")
    p.add_argument("--adv-days", type=int, default=D.adv_lookback_days)
    p.add_argument("--skip-liquidity", action="store_true")
    p.add_argument("--min-roe", type=float, default=D.min_roe_pct,
                   help="ROE%% floor applied to survivors; 0 disables")
    p.add_argument("--abs-pbr", type=float, default=D.abs_max_pbr,
                   help="absolute screen: P/B below this")
    p.add_argument("--abs-ev", type=float, default=D.abs_max_ev_ebitda,
                   help="absolute screen: EV/EBITDA below this")
    p.add_argument("--abs-strict-financials", action="store_true",
                   help="require EV/EBITDA of financials too (they have none, so "
                        "none will pass)")
    p.add_argument("--no-abs-roe", action="store_true")
    p.add_argument("--no-abs-fair-pbr", action="store_true",
                   help="drop the P/B < ROE/CoE test")
    p.add_argument("--coe", type=float, default=D.abs_cost_of_equity_pct,
                   help="cost of equity %% for the fair-P/B test")
    p.add_argument("--abs-min-div", type=float, default=D.abs_min_div_yield,
                   help="absolute screen: dividend yield %% floor; 0 disables")
    p.add_argument("--no-financials", action="store_true",
                   help="skip the 3-year revenue/EBITDA/net-profit history")
    p.add_argument("--no-history", action="store_true",
                   help="skip the own-history screen")
    p.add_argument("--hist-discount", type=float, default=D.hist_min_discount)
    p.add_argument("--hist-min-metrics", type=int, default=D.hist_min_metrics)
    p.add_argument("--dashboard", default="de_dashboard.html",
                   help="self-contained HTML dashboard; pass '' to skip")
    p.add_argument("--all", action="store_true",
                   help="write every scored row, not just passes")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    a = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    # yfinance logs every 404 at ERROR; the funnel counts them instead.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    log = logging.getLogger("de")

    keys = lambda s: tuple(k.strip() for k in s.split(",") if k.strip())  # noqa: E731
    cfg = ScreenConfig(
        min_market_cap_usd=a.min_mcap,
        min_adv_usd=0.0 if a.skip_liquidity else a.min_adv,
        discount_threshold=a.discount,
        min_metrics_passing=a.min_metrics,
        min_peers=a.min_peers,
        min_roe_pct=a.min_roe,
        abs_max_pbr=a.abs_pbr,
        abs_max_ev_ebitda=a.abs_ev,
        abs_require_roe=not a.no_abs_roe,
        abs_financials_pbr_only=not a.abs_strict_financials,
        abs_require_pbr_vs_roe=not a.no_abs_fair_pbr,
        abs_cost_of_equity_pct=a.coe,
        abs_min_div_yield=a.abs_min_div,
        hist_min_discount=a.hist_discount,
        hist_min_metrics=a.hist_min_metrics,
        peer_keys=keys(a.peer_keys),
        fallback_peer_keys=keys(a.fallback_peer_keys),
        one_line_per_company=not a.both_lines,
        exclude_investment_companies=not a.include_investment_cos,
        exclude_reits=not a.include_reits,
        exclude_property=not a.include_property,
        exclude_holdcos=a.exclude_holdcos,
        adv_lookback_days=a.adv_days,
        boards=BOARDS if a.board == "ALL" else (a.board,),
    )

    prov = XetraProvider(cfg)
    days = prov.recent_business_days(cfg.adv_lookback_days)
    asof = days[-1] if days else datetime.now().strftime("%Y-%m-%d")
    log.info("as of %s", asof)

    # 1. roster: the exchange's own workbook, one request for the market
    log.info("building roster...")
    roster = prov.listing_roster()
    if roster.empty:
        log.error("empty roster - the listed-companies workbook did not parse")
        return 1
    roster = roster[roster["board"].isin(cfg.boards)].reset_index(drop=True)
    log.info("roster: %d listings (%s)", len(roster),
             ", ".join(f"{b}={int((roster['board'] == b).sum())}" for b in cfg.boards))
    if prov.roster_asof:
        age = (pd.Timestamp(asof) - pd.Timestamp(prov.roster_asof)).days
        if age > 75:
            log.warning("the exchange's roster is %d days old (as at %s) - new "
                        "listings since then are missing", age, prov.roster_asof)

    # 2. fundamentals: one yfinance .info per line.
    #
    #    INVARIANT 7 READS AS IT DOES IN THE UK BUILD. No free source gives a
    #    German cross-section of market caps, but the roster is ~460 lines and
    #    one .info returns market cap and every multiple together, so a
    #    separate pre-gate pass would double the requests to save nothing.
    #    Everything slower - prices for liquidity, three statement calls per
    #    name - runs only on names past the size gate.
    log.info("fetching fundamentals for %d tickers...", len(roster))
    snap = prov.snapshot(roster["ticker"].tolist(), asof)

    # 2b. lines Yahoo does not know under the exchange's symbol. Resolved by
    #     ISIN and refetched; see XetraProvider.resolve_symbols for why this is
    #     not optional (Schaeffler, an MDAX company, is one of them).
    priced = set(snap.loc[snap["market_cap_local"].notna(), "ticker"])
    miss = roster[~roster["ticker"].isin(priced)]
    resolved = 0
    if not miss.empty:
        remap = prov.resolve_symbols(miss)
        for attempt in ("", "|alt"):
            m = {old: remap[old + attempt] for old in miss["ticker"]
                 if old + attempt in remap and old not in priced}
            if not m:
                continue
            s2 = prov.snapshot(list(m.values()), asof)
            ok = s2[s2["market_cap_local"].notna()]
            back = {v: k for k, v in m.items()}
            for new in ok["ticker"]:
                old = back[new]
                roster.loc[roster["ticker"] == old, "ticker"] = new
                priced.add(old)
                resolved += 1
            snap = pd.concat([snap[~snap["ticker"].isin(ok["ticker"])], ok],
                             ignore_index=True)
    log.info("  %d lines priced after ISIN resolution (%d resolved)",
             int(roster["ticker"].isin(set(snap.loc[snap["market_cap_local"].notna(),
                                                     "ticker"])).sum()), resolved)

    df = roster.merge(snap, on="ticker", how="left")
    n_priced = int(df["market_cap_local"].notna().sum())
    xetra = df["venue"].str.upper().str.contains("XETRA", na=False)
    xetra_priced = int((xetra & df["market_cap_local"].notna()).sum())
    # Refuse rather than screen a third of the market. Measured against Xetra
    # lines only: Frankfurt-only listings are mostly tiny and patchily quoted,
    # and a healthy run prices ~95% of Xetra while leaving many of them blank.
    if xetra_priced < 0.5 * int(xetra.sum()):
        log.error("only %d of %d Xetra lines priced. Yahoo is rate-limiting; the "
                  "cache has kept what arrived, so re-running in a few minutes "
                  "will fill the rest. Refusing to screen a partial universe.",
                  xetra_priced, int(xetra.sum()))
        return 2
    ustats: dict = {"roster": len(roster), "priced": n_priced,
                    "priced_via_isin": resolved,
                    "mcap_via_fast_info": int((df["mcap_source"] == "fast_info").sum())}

    # 3. German share-class hygiene
    df, gstats = DF.apply_german_filters(df, cfg)
    ustats.update(gstats)
    log.info("after German share-class hygiene: %d", len(df))

    usd_per_eur = a.fx if a.fx else prov.eur_to_usd()
    log.info("FX: 1 EUR = %.4f USD", usd_per_eur)

    # 4. size gate
    df["market_cap_usd"] = pd.to_numeric(df["market_cap_local"], errors="coerce") * usd_per_eur
    pre = df[df["market_cap_usd"] >= cfg.min_market_cap_usd].copy()
    ustats["cleared_market_cap"] = len(pre)
    log.info("%d of %d listings cleared USD %.0fm", len(pre), len(df),
             cfg.min_market_cap_usd / 1e6)
    if pre.empty:
        print("Nothing cleared the size gate.")
        return 0

    # 5. liquidity: one batched price download for the size survivors. Fetched
    #    even with --skip-liquidity, because keeping the right line of a
    #    dual-class issuer needs it.
    log.info("fetching %d days of prices for %d names...", cfg.adv_lookback_days, len(pre))
    panel = prov.price_panel(pre["ticker"].tolist(), cfg.adv_lookback_days)
    adv = prov.average_daily_value(panel, pre["ticker"].tolist(), cfg.adv_lookback_days)
    pre = pre.merge(adv, on="ticker", how="left")
    pre["adv_usd"] = pd.to_numeric(pre["adv_local"], errors="coerce") * usd_per_eur

    # 6. one line per issuer - the more liquid one
    if cfg.one_line_per_company:
        pre, lstats = DF.keep_one_line_per_issuer(pre)
        ustats.update(lstats)

    if not a.skip_liquidity:
        pre = pre[pre["adv_usd"] >= cfg.min_adv_usd]
    ustats["cleared_size_liquidity"] = len(pre)
    log.info("%d cleared size/liquidity", len(pre))
    if pre.empty:
        print("Nothing cleared the size and liquidity gates.")
        return 0

    # 7. filed statements, survivors only (invariant 7)
    if not (a.no_financials and a.no_history):
        from providers_de import fetch_statements
        log.info("fetching filed statements for %d names...", len(pre))
        st = fetch_statements(pre, cfg, asof)
        if len(st.columns) > 1:
            pre = pre.merge(st, on="ticker", how="left")
            if a.no_financials:
                pre = pre.drop(columns=[c for c in pre.columns
                                        if c.startswith(("rev_", "op_", "ebitda_", "np_",
                                                         "fin_years", "fin_n"))])

    # 8. screen
    # With --skip-liquidity cfg.min_adv_usd is 0, so apply_gates leaves adv_usd
    # alone and the measured value still reaches the dashboard.
    res, stats = run_screen(pre, usd_per_eur, cfg)
    if res.empty:
        print("Nothing survived screening.")
        return 0
    res = DF.add_quality_context(res)
    res = DF.add_valueup_flags(res)
    res = DF.flag_ttm_vs_filed(res)
    if cfg.min_roe_pct > 0:
        res, roestats = DF.apply_roe_gate(res, cfg)
        stats = {**stats, **roestats}
    else:
        res["roe_ok"], res["roe_tier"] = True, ""

    res, absstats = DF.apply_absolute_screen(res, cfg)
    stats = {**stats, **absstats}

    if not a.no_history and "hist_pbr" in res.columns:
        res, hstats = DF.apply_history_screen(res, cfg)
        stats = {**stats, **hstats}

    # Display names. The exchange's are register abbreviations ("BILFINGER SE
    # O.N.", "DT.EFF.U.WECH.-BET.G.O.N."); Yahoo's are readable. The exchange
    # name has already done its job - issuer grouping - and is kept alongside.
    res["name_exchange"] = res["name"]
    yn = res.get("yf_name", pd.Series(index=res.index, dtype=object))
    res["name"] = yn.where(yn.notna() & yn.astype(str).str.strip().ne(""), res["name"])

    funnel = {**ustats, **stats}
    print("\n--- funnel ---")
    for k, v in funnel.items():
        print(f"  {k:<28} {v}")

    out = res if a.all else res[res["passes_any"]]
    cols = [c for c in DF.de_output_columns(cfg) if c in res.columns]
    out[cols].to_csv(a.out, index=False, encoding="utf-8-sig")

    hits = res[res["passes_any"]]
    print(f"\n--- {len(hits)} stock(s) passing at least one screen ---")
    if not hits.empty:
        show = hits[["symbol", "name", "industry", "market_cap_usd",
                     "trailing_pe", "price_to_book", "roe_pct", "div_yield",
                     "avg_discount", "screen"]].head(30).copy()
        show["mcap_$m"] = (show.pop("market_cap_usd") / 1e6).round(0).astype("Int64")
        show["avg_discount"] = show["avg_discount"].map(
            lambda x: f"{x:.1%}" if pd.notna(x) else "")
        show["name"] = show["name"].str.slice(0, 28)
        print(show.to_string(index=False))

    meta = {
        "asof": asof, "source": "deutsche-boerse+yfinance", "board": a.board,
        "roster_asof": prov.roster_asof,
        "cmd": "python main_de.py " + " ".join(sys.argv[1:]),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "usd_per_eur": round(usd_per_eur, 4),
        "funnel": funnel,
        "thresholds": {
            "min_mcap_usd": cfg.min_market_cap_usd,
            "min_adv_usd": 0 if a.skip_liquidity else cfg.min_adv_usd,
            "skip_liquidity": bool(a.skip_liquidity),
            "discount": cfg.discount_threshold,
            "min_metrics": cfg.min_metrics_passing,
            "min_peers": cfg.min_peers,
            "min_valid_metrics": cfg.min_valid_metrics,
            "min_roe_pct": cfg.min_roe_pct,
            "roe_good_pct": cfg.roe_good_pct,
            "abs_max_pbr": cfg.abs_max_pbr,
            "abs_max_ev_ebitda": cfg.abs_max_ev_ebitda,
            "abs_require_roe": cfg.abs_require_roe,
            "abs_financials_pbr_only": cfg.abs_financials_pbr_only,
            "abs_require_pbr_vs_roe": cfg.abs_require_pbr_vs_roe,
            "abs_cost_of_equity_pct": cfg.abs_cost_of_equity_pct,
            "abs_min_div_yield": cfg.abs_min_div_yield,
            "hist_min_discount": cfg.hist_min_discount,
            "hist_min_metrics": cfg.hist_min_metrics,
            "hist_min_years": cfg.hist_min_years,
        },
    }
    meta_path = os.path.splitext(a.out)[0] + "_meta.json"
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(f"\nwrote {a.out} and {meta_path}")

    if a.dashboard:
        try:
            from dashboard import build_dashboard, sibling_boards
            build_dashboard(a.out, meta_path, a.dashboard,
                            boards=sibling_boards(a.board, a.dashboard))
            print(f"wrote {a.dashboard}   <- open this")
        except Exception as e:
            log.error("dashboard build failed: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
