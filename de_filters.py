"""Germany-specific filters layered on top of the generic screener.

Everything from add_valueup_flags down is the UK build's, which is in turn
Korea's: the quality floor, the absolute screen and the own-history screen are
not country-specific. Two things are local:

  * the top - which listed lines are structurally cheap on Deutsche Börse
  * the financials test, which reads the exchange's sector (config_de) rather
    than matching "Financial" in Yahoo's sector string
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

import config_de as K

log = logging.getLogger(__name__)


def _fin_mask(df: pd.DataFrame) -> pd.Series:
    """Banks, insurers and other financials, by Deutsche Börse sector.

    Korea had to match the substring "Financial" in a vendor sector string.
    Here the exchange states it, as in Japan - with one carve-out: the
    exchange files property companies under Financial Services, and their
    EV/EBITDA is meaningful, so Real Estate is not a financial.
    """
    sec = df.get("sector", pd.Series("", index=df.index)).fillna("")
    ind = df.get("industry", pd.Series("", index=df.index)).fillna("")
    return pd.Series([K.is_financial(a, b) for a, b in zip(sec, ind)], index=df.index)


def tag_share_classes(df: pd.DataFrame) -> pd.DataFrame:
    """Tag every line whose cheapness may be structural rather than a mispricing."""
    df = df.copy()
    name = df["name"].fillna("").astype(str)
    sym = df.get("symbol", df["ticker"].str.replace(r"\.(DE|F)$", "", regex=True)) \
            .fillna("").astype(str).str.upper()
    ind = df.get("industry", pd.Series("", index=df.index)).fillna("").astype(str)
    yind = df.get("yf_industry", pd.Series("", index=df.index)).fillna("").astype(str)
    qtype = df.get("quote_type", pd.Series("", index=df.index)).fillna("").astype(str)
    country = df.get("country", pd.Series("Germany", index=df.index)).fillna("").astype(str)
    tier = df.get("tier", pd.Series("", index=df.index)).fillna("").astype(str)

    # Vorzugsaktien: by the exchange's own name ("VZO", "Vz") or by the Xetra
    # convention that a trailing 3 is the Vorzug of the base symbol - trusted
    # only when that base is itself in the roster (VOW3 vs VOW), since plenty
    # of ordinary symbols merely end in 3.
    present = set(sym)
    by_symbol = sym.str.endswith("3") & sym.str[:-1].isin(present)
    df["is_pref"] = name.map(K.is_pref_name) | by_symbol

    df["issuer"] = [K.issuer_key(i, n) for i, n in
                    zip(df.get("isin", pd.Series("", index=df.index)).fillna(""), name)]
    df["n_lines"] = df.groupby("issuer")["issuer"].transform("size")
    # A Vorzug that is its issuer's only listed line - Porsche SE, Porsche AG,
    # Jungheinrich, Drägerwerk on Xetra. Kept (there is no other price for the
    # company), but marked, because the voting line is held privately and the
    # minority owns no say over the capital it is valuing.
    df["is_pref_only"] = df["is_pref"] & (df["n_lines"] == 1)

    df["is_investment_co"] = ind.map(K.is_investment_company)
    df["is_reit"] = name.map(K.is_reit) | yind.str.contains("REIT", case=False, na=False)
    df["is_property"] = ind.map(K.is_property) & ~df["is_reit"]
    df["is_holdco"] = name.map(K.is_holdco)
    # A foreign issuer that is not in a Deutsche Börse index is a secondary
    # line: UniCredit, Haier's D-shares, BB Biotech. Its price is set in Milan,
    # Shanghai or Zurich and the Xetra quote follows - and Haier's D-shares
    # trade at a standing discount to its A-shares that is a market-access
    # artefact, not a valuation. Index membership is the exchange's own
    # judgement that a foreign issuer's main market IS here (Airbus, Qiagen,
    # Aroundtown, Redcare), so those stay.
    df["is_foreign_secondary"] = country.ne("Germany") & country.ne("") & tier.eq("")
    df["is_fund_quote"] = qtype.str.upper().isin(["ETF", "MUTUALFUND", "FUND"])
    return df


def apply_german_filters(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Remove lines whose discount is structural, not a mispricing.

    Listed private equity and holding vehicles are the German analogue of the
    UK's investment trusts: their book is a portfolio of stakes valued at a
    discount to NAV that is the normal state of a closed-end vehicle. The
    exchange names the class (subsector "Private Equity & Venture Capital"),
    so unlike the UK no register or heuristic is needed.

    REITs are excluded for the reason Korea excluded them, and so are listed
    landlords, whose IAS 40 revaluations make their P/E an appraisal rather
    than an earnings multiple (config_de.PROPERTY_SUBSECTORS). Foreign secondary
    lines and fund quotes have no business in a German screen at all.

    One line per issuer is NOT applied here - it needs traded value, which is
    only fetched for names past the size gate. See keep_one_line_per_issuer.
    """
    df = tag_share_classes(df)
    stats = {"listings": len(df)}

    def drop(flag: str, key: str, on: bool):
        nonlocal df
        if not on:
            return
        n = int(df[flag].sum())
        df = df[~df[flag]]
        stats[key] = n

    drop("is_investment_co", "dropped_investment_co", cfg.exclude_investment_companies)
    drop("is_reit", "dropped_reit", cfg.exclude_reits)
    drop("is_property", "dropped_property", cfg.exclude_property)
    drop("is_foreign_secondary", "dropped_foreign_secondary", True)
    drop("is_fund_quote", "dropped_fund_quote", True)

    if cfg.exclude_holdcos:
        n = int(df["is_holdco"].sum())
        df = df[~df["is_holdco"]]
        stats["dropped_holdco"] = n
    else:
        stats["flagged_holdco"] = int(df["is_holdco"].sum())

    stats["after_german_filters"] = len(df)
    return df.copy(), stats


def keep_one_line_per_issuer(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Stamm and Vorzug of one company: keep the line the market actually trades.

    This is Korea's 우선주 invariant, and the German version of the trap has a
    different shape. In Seoul the preferred is always the illiquid, discounted
    line and dropping it is the whole fix. In Frankfurt it is often the other
    way round - VW, Henkel, Sartorius and Fuchs trade 3 to 30 times more in
    the Vorzug than in the Stamm - and the gap between the two lines runs either
    way: Sixt's Vorzug sits ~19% below its Stamm, Sartorius's ~24% ABOVE.

    Two lines of one company in one screen are wrong whichever is cheaper:
    both carry the same EPS and book, so the cheaper line is scored "cheap"
    against a peer cohort that contains its own sibling. So exactly one line
    per issuer survives, and it is the one with the higher median traded
    value. Not the index line: the exchange file marks VW's, Sartorius's and
    Fuchs's Stamm as the index member, while their Vorzug is where the volume
    is, so the index flag would keep the line nobody trades.

    Market cap is the same on both lines (Yahoo prices all shares of both
    classes at this line's price), so the size gate is unaffected by which is
    kept. Ties without volume data fall back to the Stamm.
    """
    df = df.copy()
    adv = pd.to_numeric(df.get("adv_local"), errors="coerce").fillna(-1.0)
    df["_rank_adv"] = adv
    df["_rank_common"] = (~df["is_pref"].astype(bool)).astype(int)
    df = df.sort_values(["issuer", "_rank_adv", "_rank_common"],
                        ascending=[True, False, False])
    dup = df.duplicated(subset=["issuer"], keep="first")
    dropped = df.loc[dup, "ticker"].tolist()
    kept_pref = df.loc[~dup & (df["n_lines"] > 1) & df["is_pref"].astype(bool), "ticker"].tolist()
    if dropped:
        log.info("one line per issuer: dropped %s; kept the Vorzug for %s",
                 ", ".join(dropped), ", ".join(kept_pref) or "none")
    df = df[~dup].drop(columns=["_rank_adv", "_rank_common"])
    return df, {"dropped_second_line": len(dropped),
                "kept_vorzug_over_stamm": len(kept_pref)}


def add_valueup_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Low-P/B flags.

    Korea had a live policy catalyst attached to almost exactly this screen -
    KRX publicly identifies firms whose PBR sits in the bottom 20% of their
    industry. Germany has no counterpart: no disclosure regime is keyed to
    valuation. What closes German discounts is usually a bid, and German
    takeover law makes that slower than in London (domination agreements,
    squeeze-out thresholds), none of which is computable from this data.

    So these two columns are kept for continuity and for sorting, not as a
    catalyst.
    """
    df = df.copy()
    rank = df.get("price_to_book_pct_rank")
    df["pbr_bottom20_industry"] = (rank <= 0.20) if rank is not None else False
    df["pbr_below_1"] = df["price_to_book"] < 1.0
    return df


def add_quality_context(df: pd.DataFrame) -> pd.DataFrame:
    """ROE derived from EPS and BPS rather than taken as a vendor field.

    Same reasoning as Korea: the ratio is then consistent with the very P/E
    and P/B being screened on, and it is unit-free, so a reporting currency that
    differs from the quote currency (Qiagen) cannot reach it.
    """
    df = df.copy()
    eps = pd.to_numeric(df.get("trailing_eps"), errors="coerce")
    bps = pd.to_numeric(df.get("book_value_ps"), errors="coerce")
    df["roe_pct"] = np.where((bps > 0) & eps.notna(), eps / bps * 100.0, np.nan)
    df["div_yield"] = pd.to_numeric(df.get("div_yield"), errors="coerce")
    df["pays_dividend"] = df["div_yield"].fillna(0) > 0
    return df


def flag_ttm_vs_filed(df: pd.DataFrame) -> pd.DataFrame:
    """Profitable over the trailing twelve months, loss-making in the last
    filed year. A flag, never a gate.

    The peer and absolute screens use Yahoo's trailing multiples in every
    market, and Yahoo's trailing twelve months is the sum of the last four
    quarters - one-offs included. K+S is the case that found this: two
    quarters carrying very large non-operating gains (Q4 2025, Q2 2026) turn
    a filed 2025 loss of EUR 1.1bn (normalized) into TTM net income of
    +EUR 1.06bn, so Yahoo reports P/E 2.6 and ROE 20% and K+S tops the peer
    screen on accounting rather than business. Quarterly EBITDA of 748m
    against ~640m for the whole of 2025 is the tell.

    It is not a sign error - the quarters genuinely sum that way - so the
    trailing figure is left alone and the row is marked instead. The test is
    deliberately narrow: TTM profit against a normalized filed LOSS. A ratio
    test (TTM P/E far below filed P/E) would also catch genuine turnarounds -
    Siemens Energy and RWE on the 2026-10-02 run - which is noise, not a
    warning. Measured on that run it flags two names, K+S and Salzgitter.
    """
    df = df.copy()
    pe = pd.to_numeric(df.get("trailing_pe"), errors="coerce")
    lf = pd.to_numeric(df.get("lf_ni"), errors="coerce")
    df["ttm_vs_filed_loss"] = pe.notna() & (lf < 0)
    return df


def apply_roe_gate(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Require survivors to actually earn something.

    Runs after scoring, never before: it narrows `passes` and leaves
    avg_discount alone, so the peer cohorts still contain the low-ROE names
    that make them representative. Missing ROE fails.
    """
    df = df.copy()
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    df["roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)
    df["roe_tier"] = np.select(
        [roe >= 15.0, roe >= cfg.roe_good_pct, roe >= cfg.min_roe_pct],
        ["strong", "good", "marginal"], default="fail")

    before = int(df["passes"].sum())
    df["passes"] = df["passes"] & df["roe_ok"]
    after = int(df["passes"].sum())
    stats = {f"dropped_roe_below_{cfg.min_roe_pct:g}": before - after,
             "passing_after_roe": after}
    df = df.sort_values(["passes", "avg_discount"], ascending=[False, False])
    return df, stats


def apply_absolute_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Absolute cheapness, independent of the peer comparison.

    Identical in structure to the Korea version, including the financials
    carve-out: EV/EBITDA is suppressed for banks and insurers, so a strict
    both-metrics rule would exclude every German bank and insurer - Deutsche
    Bank and Commerzbank among them, which is where below-book names are.
    """
    df = df.copy()
    pbr = pd.to_numeric(df.get("price_to_book"), errors="coerce")
    ev = pd.to_numeric(df.get("ev_to_ebitda"), errors="coerce")
    roe = pd.to_numeric(df.get("roe_pct"), errors="coerce")
    dy = pd.to_numeric(df.get("div_yield"), errors="coerce")
    fin = _fin_mask(df)

    df["abs_pbr_ok"] = pbr.notna() & (pbr < cfg.abs_max_pbr)
    df["abs_ev_ok"] = ev.notna() & (ev < cfg.abs_max_ev_ebitda)
    df["abs_ev_missing"] = ev.isna()
    df["abs_roe_ok"] = roe.notna() & (roe >= cfg.min_roe_pct)

    fair_pbr = roe / cfg.abs_cost_of_equity_pct
    df["abs_fair_pbr"] = fair_pbr
    df["abs_pbr_vs_roe_ok"] = pbr.notna() & fair_pbr.notna() & (pbr < fair_pbr)
    df["abs_div_ok"] = dy.notna() & (dy >= cfg.abs_min_div_yield)

    core = df["abs_pbr_ok"] & df["abs_ev_ok"]
    if cfg.abs_financials_pbr_only:
        core = core | (fin & df["abs_pbr_ok"])
        df["abs_via_carveout"] = fin & df["abs_pbr_ok"] & ~df["abs_ev_ok"]
    else:
        df["abs_via_carveout"] = False

    if cfg.abs_require_roe:
        core = core & df["abs_roe_ok"]
    if cfg.abs_require_pbr_vs_roe:
        core = core & df["abs_pbr_vs_roe_ok"]
    if cfg.abs_min_div_yield > 0:
        core = core & df["abs_div_ok"]
    df["abs_passes"] = core

    rel = df["passes"].astype(bool)
    absp = df["abs_passes"].astype(bool)
    df["screen"] = np.select([rel & absp, rel & ~absp, ~rel & absp],
                             ["both", "relative", "absolute"], default="")
    df["passes_any"] = rel | absp

    stats = {
        f"abs_pbr_under_{cfg.abs_max_pbr:g}": int(df["abs_pbr_ok"].sum()),
        f"abs_ev_under_{cfg.abs_max_ev_ebitda:g}": int(df["abs_ev_ok"].sum()),
        "abs_pbr_below_fair": int(df["abs_pbr_vs_roe_ok"].sum()),
        f"abs_div_over_{cfg.abs_min_div_yield:g}pct": int(df["abs_div_ok"].sum()),
        "abs_passing": int(absp.sum()),
        "abs_via_financial_carveout": int((absp & df["abs_via_carveout"]).sum()),
        "abs_new_vs_relative": int((absp & ~rel).sum()),
        "passing_either_screen": int(df["passes_any"].sum()),
    }
    df = df.sort_values(["passes_any", "avg_discount"], ascending=[False, False])
    return df, stats


def restate_info_for_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Yahoo's .info per-share fields on today's share basis, after a split.

    The peer and absolute screens read P/E, P/B, EPS, book value and yield
    straight from .info. Yahoo restates those for splits on its own schedule,
    and not consistently: after Johnson Matthey's Aug 2026 4-for-3
    consolidation its bookValue was already on today's 125.9m shares (16.01 =
    FY2026 equity / 125.9m) while its filed statements were still on the old
    167.9m; in the Japan build bookValue was restated while dividendRate was
    not. So the basis is tested per row, never assumed either way.

    `share_basis_g` comes from build_statement_record: the split / consolidation
    factor since the latest filing that today's market cap demands (1 for
    almost everyone). Where it is not 1, filed equity over (book value x filed
    shares) says which basis .info is on: ~1 means the old one, ~g the new.

      old basis   EPS, book value and yield divided by g; P/E and P/B
                  multiplied by it. ROE, their ratio, does not move.
      new basis   per-share fields are right; the dividend cannot be checked
                  the same way (in Japan it lagged when book value did not),
                  so the yield is left missing rather than trusted.
      neither     P/E, P/B and yield missing - no basis to vouch for.

    EV/EBITDA needs nothing: Yahoo builds EV from today's market cap.
    """
    df = df.copy()
    g = pd.to_numeric(df.get("share_basis_g"), errors="coerce").fillna(1.0)
    eq = pd.to_numeric(df.get("lf_equity"), errors="coerce")
    sh = pd.to_numeric(df.get("lf_shares"), errors="coerce")
    bv = pd.to_numeric(df.get("book_value_ps"), errors="coerce")
    k = eq / (bv * sh)
    moved = np.abs(np.log(g)) > 0.02
    tol = np.log(1.25)
    old = moved & (np.abs(np.log(k)) < tol)
    new = moved & ~old & (np.abs(np.log(k / g)) < tol)
    unclear = moved & ~old & ~new

    for c, op in (("trailing_eps", "div"), ("book_value_ps", "div"), ("div_yield", "div"),
                  ("trailing_pe", "mul"), ("price_to_book", "mul")):
        if c in df.columns:
            v = pd.to_numeric(df[c], errors="coerce")
            df[c] = v.where(~old, v / g if op == "div" else v * g)
    if "div_yield" in df.columns:
        df.loc[new, "div_yield"] = np.nan
    for c in ("trailing_pe", "price_to_book", "div_yield"):
        if c in df.columns:
            df.loc[unclear, c] = np.nan

    df["split_note"] = np.select([old, new, unclear],
                                 ["info restated for split", "yield unverified after split",
                                  "info basis unclear"], default="")
    stats = {"info_restated_for_split": int(old.sum()),
             "info_yield_unverified": int(new.sum()),
             "info_basis_unclear": int(unclear.sum())}
    if moved.any():
        log.info("split basis: %d .info rows restated, %d yields unverified, %d unclear",
                 *stats.values())
    return df, stats


HIST_METRICS = (("per", "trailing_pe"), ("pbr", "price_to_book"),
                ("evx", "ev_to_ebitda"))


def _as_list(v) -> list:
    return list(v) if isinstance(v, (list, tuple, np.ndarray)) else []


def add_history_now(df: pd.DataFrame) -> pd.DataFrame:
    """Today's P/E, P/B and EV/EBITDA on the SAME basis as the history.

    Each historical year is that year-end market value over that year's filed
    totals (providers_de.build_statement_record). Today's value has to be
    built the same way - today's market value over the latest filing - or the
    comparison measures the gap between two definitions rather than a change
    in valuation.

    That is not hypothetical. yfinance's own trailingPE divides by the last
    twelve months, including interims: the UK build measured Shell at 10.5
    against 15.2 on the filed-year basis, a 31% gap that would read as a 31%
    discount to history on its own. Korea learned the same lesson on
    EV/EBITDA, where mixing providers put only 18 of 30 within +/-25%; its fix
    was to rebuild the current value from the history's own source, which is
    what this does.

    So these three columns feed the own-history screen ONLY. The peer and
    absolute screens keep yfinance's figures, because they compare companies
    with each other on a common vendor definition, not a company with itself.

    Out-of-bounds values become NaN here rather than in the screen, so the
    dashboard receives exactly what Python tested and cannot disagree with it.
    """
    df = df.copy()
    n = lambda c: pd.to_numeric(df.get(c), errors="coerce")  # noqa: E731
    mv = n("market_cap_local") * n("fx_now")      # quote-major -> reporting ccy
    ni, eq, eb = n("lf_ni"), n("lf_equity"), n("lf_ebitda")
    mi = n("lf_mi").fillna(0.0)
    df["per_now"] = (mv / ni).where(ni > 0)
    df["pbr_now"] = (mv / eq).where(eq > 0)
    df["evx_now"] = ((mv + n("lf_debt") - n("lf_cash") + mi) / eb).where(eb > 0)
    df.loc[_fin_mask(df), "evx_now"] = np.nan          # invariant 6
    for col, (_, bound_key) in zip(("per_now", "pbr_now", "evx_now"), HIST_METRICS):
        lo, hi = K.METRIC_BOUNDS[bound_key]
        df[col] = df[col].where((df[col] >= lo) & (df[col] <= hi))
    return df


def apply_history_screen(df: pd.DataFrame, cfg: K.ScreenConfig) -> tuple[pd.DataFrame, dict]:
    """Cheap against the company's own filed history - the third screen.

    A port of the Korea build's apply_history_screen, held to the same rules:
    the benchmark is a median (invariant 4); a non-positive or out-of-bounds
    multiple is missing, never cheap, in history as well as today
    (invariant 2), so a loss year drops out of the benchmark rather than
    dragging it; EV/EBITDA is skipped for financials (invariant 6); fewer than
    `hist_min_years` usable years is no benchmark; the ROE floor applies.

    One difference, and it is a data limit rather than a choice: Yahoo carries
    FOUR filed years for German companies, as for UK ones, where WiseReport
    gave Korea five, so the benchmark is a four-year median.
    """
    df = add_history_now(df)
    fin = _fin_mask(df)

    disc_cols = []
    for key, bound_key in HIST_METRICS:
        lo, hi = K.METRIC_BOUNDS[bound_key]
        vals = df.get(f"hist_{key}", pd.Series([[]] * len(df), index=df.index)) \
                 .map(_as_list)
        for i in range(5):
            df[f"hist_{key}_y{i + 1}"] = vals.map(
                lambda v, i=i: v[i] if i < len(v) and v[i] is not None else np.nan)

        def bench(v):
            ok = [float(x) for x in v if x is not None and np.isfinite(float(x))
                  and lo <= float(x) <= hi]
            return float(np.median(ok)) if len(ok) >= cfg.hist_min_years else np.nan

        med = vals.map(bench)
        if key == "evx":
            med = med.where(~fin)
        cur = df[f"{key}_now"]
        df[f"hist_{key}_med"] = med
        df[f"hist_{key}_disc"] = (med - cur) / med
        disc_cols.append(f"hist_{key}_disc")

    discs = df[disc_cols]
    df["hist_n_valid"] = discs.notna().sum(axis=1)
    df["hist_n_pass"] = (discs >= cfg.hist_min_discount).sum(axis=1)
    # Averages every metric with data, including the failing ones - the same
    # rule as avg_discount (invariant 5).
    df["hist_avg_disc"] = discs.mean(axis=1, skipna=True)

    roe_ok = df.get("roe_ok", pd.Series(True, index=df.index)).fillna(False).astype(bool)
    hp = df["hist_n_pass"] >= cfg.hist_min_metrics
    if cfg.hist_require_roe:
        hp = hp & roe_ok
    df["hist_passes"] = hp

    # Three screens now, so `screen` names every one a row cleared.
    rel = df["passes"].astype(bool)
    absp = df.get("abs_passes", pd.Series(False, index=df.index)).astype(bool)
    parts = pd.DataFrame({"relative": rel, "absolute": absp, "history": hp})
    df["screen"] = parts.apply(lambda r: " + ".join(k for k, v in r.items() if v), axis=1)
    df["passes_any"] = rel | absp | hp

    note = df.get("hist_note", pd.Series("", index=df.index)).fillna("")
    stats = {
        "hist_with_benchmark": int((df["hist_n_valid"] > 0).sum()),
        # Every name a guard refused a benchmark, by reason - so a data problem
        # shows up as a count in the funnel rather than as names quietly
        # missing from the third screen.
        "hist_share_break": int((note == "share-count break").sum()),
        "hist_basis_mismatch": int((note == "price/share basis mismatch").sum()),
        "hist_stale_filing": int((note == "stale filings").sum()),
        "hist_no_reporting_ccy": int((note == "no reporting currency").sum()),
        "hist_normalized_earnings": int((df.get("hist_earnings", pd.Series("", index=df.index))
                                         == "normalized").sum()),
        f"hist_passing_{cfg.hist_min_discount:.0%}": int(hp.sum()),
        "hist_new_vs_other_screens": int((hp & ~rel & ~absp).sum()),
        "passing_any_screen": int(df["passes_any"].sum()),
    }
    df = df.sort_values(["passes_any", "avg_discount"], ascending=[False, False])
    return df, stats


def de_output_columns(cfg: K.ScreenConfig) -> list[str]:
    cols = ["ticker", "symbol", "isin", "name", "name_exchange", "board", "tier", "sector", "industry",
            "yf_industry", "country", "market_cap_usd", "mcap_source", "adv_usd",
            "close_local", "currency", "is_pref", "is_pref_only"]
    for m in cfg.metrics:
        cols += [m, f"{m}_peer_median", f"{m}_discount",
                 f"{m}_peer_n", f"{m}_pct_rank"]
    # Own filed history: today's same-basis values, the median benchmark, the
    # discount to it, and each year's value for the tooltip.
    cols += ["hist_years", "hist_note", "hist_earnings", "share_basis_g", "split_note", "per_now", "pbr_now", "evx_now",
             "hist_n_valid", "hist_n_pass", "hist_avg_disc", "hist_passes"]
    for m in ("per", "pbr", "evx"):
        cols += [f"hist_{m}_med", f"hist_{m}_disc"]
        cols += [f"hist_{m}_y{i}" for i in range(1, 6)]
    # Three-year history: millions of the REPORTING currency, oldest first.
    cols += ["fin_years", "fin_n", "fin_ccy"]
    for m in ("rev", "op", "ebitda", "np"):
        cols += [f"{m}_y1", f"{m}_y2", f"{m}_y3", f"{m}_cagr"]
    cols += ["screen", "passes_any", "abs_passes", "abs_pbr_ok", "abs_ev_ok",
             "abs_pbr_vs_roe_ok", "abs_div_ok", "abs_roe_ok", "abs_fair_pbr",
             "abs_via_carveout", "ttm_vs_filed_loss",
             "roe_pct", "roe_ok", "roe_tier", "div_yield", "pays_dividend",
             "pbr_below_1", "pbr_bottom20_industry", "is_holdco",
             "n_valid_metrics", "n_metrics_passing", "metrics_passing",
             "avg_discount", "median_pct_rank", "passes"]
    return cols
