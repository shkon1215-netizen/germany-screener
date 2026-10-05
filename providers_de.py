"""Data providers for the Germany (Deutsche Börse) screener.

  roster       Deutsche Börse's own listed-companies workbook, one sheet per
               segment (Prime Standard, General Standard, Scale, Basic Board).
               Every row carries ISIN, Xetra trading symbol, the exchange's
               sector AND subsector, country of domicile and index membership
               (DAX / MDAX / SDAX, TecDAX). This is the German counterpart of
               Korea's KIND and Japan's data_j.xlsx: the whole exchange, with an
               official classification, from the operator itself. The UK had
               nothing like it and had to bound its roster by index.

  fundamentals yfinance .info on the Xetra line (SYMBOL.DE), one call per
               ticker, with fast_info as the market-cap fallback - see
               snapshot(). Same shape as the UK build, because Yahoo is the
               only free source of German per-share fundamentals.

  liquidity    one batched yf.download for the whole priced universe, as
               Japan does it: a true median daily traded value over the
               lookback, not the UK's 3-month averageVolume proxy. Xetra
               volume only - see average_daily_value.

  statements   Yahoo annual income statement + balance sheet + seven years of
               daily closes, for the 3-year history and the own-history
               screen. build_statement_record is the UK build's, unchanged.

THE MARKET-CAP TRAP: Yahoo's .info on a German line sometimes answers with
every multiple filled in and marketCap missing - Allianz (ALV.DE) and 1&1
(1U1.DE) did on 2026-10-04. A screen that reads a missing market cap as "too
small" would quietly drop Germany's second-largest company. fast_info
recomputes it from the last price and Yahoo's share-count history (Allianz
EUR 158bn), so it is the fallback, and every row records which path it took
(`mcap_source`) so the funnel can count it.

DUAL-CLASS MARKET CAPS: on a Stamm/Vorzug issuer Yahoo's marketCap is this
line's price times ALL shares of both classes (VW: 67.8 x 501m implied shares
= EUR 34.0bn on either line). That is the right number for a size gate - it is
the company's size - and the share-class filter keeps one line per issuer so
it is never counted twice.
"""
from __future__ import annotations

import concurrent.futures as cf
import io
import json
import logging
import os
import re
import time

import numpy as np
import pandas as pd
import requests

import config_de as K
from config_de import ScreenConfig

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HEADERS = {"User-Agent": UA}

LISTED_PAGE = ("https://www.cashmarket.deutsche-boerse.com/cash-en/Data-Tech/"
               "statistics/listed-companies")
# Known-good as of 2026-10-04. The blob id changes when the exchange uploads a
# new month, so the page is read first and this is only the fallback.
LISTED_FALLBACK = ("https://www.cashmarket.deutsche-boerse.com/resource/blob/67858/"
                   "3db127ac20c83f2955d06a43eddafede/data/Listed-companies.xlsx")
YAHOO_SUFFIX = ".DE"

# Every field snapshot() promises to return. Declared rather than inferred so a
# fully throttled fetch still produces a correctly-shaped frame.
SNAPSHOT_FIELDS = (
    "yf_name", "currency", "market_cap_local", "mcap_source", "close_local",
    "trailing_pe", "price_to_book", "ev_to_ebitda", "trailing_eps",
    "book_value_ps", "div_yield", "yf_sector", "yf_industry", "yf_country",
    "quote_type", "yf_shares",
    # The currency the ACCOUNTS are in. Qiagen trades in euros on Xetra and
    # reports in US dollars; anything dividing a market value by a reported
    # figure needs both.
    "fin_ccy",
)


def out_schema(df: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        df = pd.DataFrame({"ticker": tickers})
    df = df.reindex(columns=["ticker"] + list(SNAPSHOT_FIELDS))
    return df[df["ticker"].isin(tickers)].reset_index(drop=True)


def to_yahoo(symbol: str, venue: str = "XETRA") -> str:
    """Exchange trading symbol -> Yahoo symbol. VOW3 -> VOW3.DE.

    Lines the exchange lists as tradeable on Frankfurt only (no Xetra) have no
    .DE quote at all - Yahoo answers 404 - so they take the Frankfurt suffix.
    The symbol itself is a first guess either way: see resolve_symbols for the
    ~30 lines where Yahoo's symbol differs from the exchange's.
    """
    t = re.sub(r"[^A-Z0-9]", "", str(symbol).strip().upper())
    if not t:
        return ""
    return f"{t}.F" if "XETRA" not in str(venue).upper() else f"{t}{YAHOO_SUFFIX}"


class XetraProvider:
    def __init__(self, cfg: ScreenConfig):
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.roster_asof = ""

    # -- calendar ----------------------------------------------------------
    def recent_business_days(self, n: int) -> list[str]:
        """Real Xetra sessions, read off a liquid line - German holidays
        (Whit Monday, Day of German Unity, 24 Dec) match no generic calendar."""
        import yfinance as yf
        try:
            h = yf.Ticker("SAP.DE").history(period=f"{max(n * 2, 120)}d")
            if not h.empty:
                return [d.strftime("%Y-%m-%d") for d in h.index[-n:]]
        except Exception as e:
            log.warning("calendar via yfinance failed: %s", e)
        days = pd.bdate_range(end=pd.Timestamp.today(), periods=n)
        return [d.strftime("%Y-%m-%d") for d in days]

    # -- roster ------------------------------------------------------------
    def _roster_url(self) -> str:
        try:
            html = self.s.get(LISTED_PAGE, timeout=40).text
            m = re.search(r'href="([^"]*Listed-companies[^"]*\.xlsx)"', html)
            if m:
                href = m.group(1)
                return href if href.startswith("http") else \
                    "https://www.cashmarket.deutsche-boerse.com" + href
            log.warning("listed-companies link not found on the page; using the "
                        "known URL")
        except Exception as e:
            log.warning("listed-companies page failed (%s); using the known URL", e)
        return LISTED_FALLBACK

    def listing_roster(self) -> pd.DataFrame:
        """Every instrument on every Deutsche Börse equity segment.

        The workbook carries a header block (title, "as at" date, counts) above
        the table on each sheet, so the header row is located by its "ISIN"
        cell rather than assumed - the exchange has moved it before in other
        files and the UK build learned not to trust positions.
        """
        cols = ["ticker", "symbol", "isin", "name", "board", "sector", "industry",
                "country", "tier", "tecdax", "venue"]
        url = self._roster_url()
        try:
            r = self.s.get(url, timeout=60)
            r.raise_for_status()
            book = pd.ExcelFile(io.BytesIO(r.content))
        except Exception as e:
            log.error("listed-companies workbook failed: %s", e)
            return pd.DataFrame(columns=cols)

        frames = []
        for sheet, board in K.SEGMENTS.items():
            if sheet not in book.sheet_names:
                log.warning("  sheet %r missing from the workbook", sheet)
                continue
            raw = pd.read_excel(book, sheet, header=None, dtype=str)
            hdr = raw.index[raw.iloc[:, 0].astype(str).str.strip().eq("ISIN")]
            if hdr.empty:
                log.warning("  no ISIN header on sheet %r", sheet)
                continue
            # The "as at" stamp sits in column 0 above the header.
            if not self.roster_asof:
                for v in raw.iloc[:hdr[0], 0].dropna():
                    m = re.match(r"(\d{4}-\d{2}-\d{2})", str(v).strip())
                    if m:
                        self.roster_asof = m.group(1)
                        break
            t = raw.iloc[hdr[0] + 1:].copy()
            t.columns = [str(c).strip() for c in raw.iloc[hdr[0]]]
            t = t[t["ISIN"].notna() & t["ISIN"].astype(str).str.match(r"^[A-Z]{2}[A-Z0-9]{9}\d$")]

            def col(name):
                return t[name] if name in t.columns else pd.Series("", index=t.index)

            out = pd.DataFrame({
                "isin": col("ISIN").astype(str).str.strip(),
                "symbol": col("Trading Symbol").astype(str).str.strip().str.upper(),
                "name": col("Company").astype(str).str.replace("\xa0", " ").str.strip(),
                "sector": col("Sector").map(K.norm_sector),
                "industry": col("Subsector").map(K.norm_subsector),
                "country": col("Country").astype(str).str.strip()
                                            .replace({"Luxemburg": "Luxembourg"}),
                "venue": col("Instrument Exchange").astype(str).str.strip(),
                "tier": col("Index").fillna("").astype(str).str.strip()
                                    .replace({"-": "", "nan": ""}),
                "tecdax": col("TecDAX").fillna("").astype(str).str.upper()
                                       .str.contains("TECDAX"),
            })
            out["board"] = board
            log.info("  %-16s %3d instruments", sheet, len(out))
            frames.append(out)

        if not frames:
            return pd.DataFrame(columns=cols)
        df = pd.concat(frames, ignore_index=True)
        df["ticker"] = [to_yahoo(s, v) for s, v in zip(df["symbol"], df["venue"])]
        df = df[df["ticker"].ne("")]
        # One instrument appears on one segment only, but guard anyway: a
        # segment change mid-month must not count a company twice.
        df = df.drop_duplicates(subset=["isin"], keep="first")
        log.info("  roster as at %s", self.roster_asof or "unknown")
        return df[cols].reset_index(drop=True)

    # -- symbol resolution -------------------------------------------------
    def resolve_symbols(self, rows: pd.DataFrame) -> dict[str, str]:
        """Yahoo symbols for lines whose exchange symbol Yahoo does not know.

        The exchange file and Yahoo disagree for ~30 lines, and the misses are
        not obscure: Schaeffler (MDAX) is SHA on the exchange and SHA0 on Yahoo
        since the Vitesco merger re-issued its shares; Uniper is UN01 and
        UN0.DE; Einhell's Vorzug is EIN3 and EIN.DE after its 2024 split. A
        miss reads as "no market cap", which is indistinguishable from "too
        small" - so an MDAX company would fail the size gate silently.

        Yahoo's search resolves an ISIN exactly. Xetra quotes are preferred,
        then Frankfurt; a hit only on a foreign exchange (Vulcan Energy on the
        ASX) is left unresolved, because that line's primary market is not
        here. Results are cached for a week - an ISIN-to-symbol mapping
        changes on a corporate action, not daily.

        Returns {roster ticker: resolved Yahoo symbol}, only for lines that
        resolved to something different.
        """
        import yfinance as yf
        path = os.path.join(self.cfg.cache_dir, "isin_symbols.json")
        cache: dict = {}
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < 7 * 86400:
            try:
                with open(path, encoding="utf-8") as fh:
                    cache = json.load(fh)
            except Exception:
                cache = {}

        out: dict[str, str] = {}
        for _, r in rows.iterrows():
            isin, old = str(r["isin"]), str(r["ticker"])
            if isin not in cache:
                try:
                    quotes = yf.Search(isin, max_results=8, news_count=0).quotes or []
                    syms = [str(q.get("symbol") or "") for q in quotes]
                except Exception as e:
                    log.debug("ISIN search %s: %s", isin, e)
                    continue
                de = [x for x in syms if x.endswith(".DE")]
                fr = [x for x in syms if x.endswith(".F")]
                # A Frankfurt-only hit for a line the exchange says trades on
                # Xetra usually means Yahoo files the Xetra quote under the same
                # base - Schaeffler's search returns only SHA0.F, and SHA0.DE
                # exists. Try it first: Xetra is where the volume is.
                cand = de or ([fr[0][:-2] + ".DE"] if fr and "XETRA" in str(r.get("venue", "")).upper() else [])
                cand += fr
                cache[isin] = cand
                time.sleep(self.cfg.request_delay)
            cand = cache.get(isin) or []
            if cand and cand[0] != old:
                out[old] = cand[0]
                if len(cand) > 1:
                    out[old + "|alt"] = cand[1]
        try:
            os.makedirs(self.cfg.cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
        except Exception as e:
            log.warning("  could not write ISIN cache: %s", e)
        n = len([k for k in out if not k.endswith("|alt")])
        log.info("  resolved %d of %d unpriced lines by ISIN", n, len(rows))
        return out

    # -- fundamentals ------------------------------------------------------
    def _cache_path(self, asof: str) -> str:
        return os.path.join(self.cfg.cache_dir, f"snapshot_{asof or 'latest'}.csv")

    def snapshot(self, tickers: list[str], asof: str = "") -> pd.DataFrame:
        """One yfinance .info per ticker, fast_info when .info has no market cap.

        Cached per session date for the reason the UK build found: Yahoo
        throttles by returning dicts with fields MISSING, not by raising, so
        successive runs must fill gaps rather than re-ask for everything.
        """
        import yfinance as yf

        cached = pd.DataFrame()
        path = self._cache_path(asof)
        if os.path.exists(path):
            age_h = (time.time() - os.path.getmtime(path)) / 3600.0
            if age_h <= self.cfg.cache_ttl_hours:
                try:
                    cached = pd.read_csv(path)
                    log.info("  cache: %d rows from %s (%.1fh old)",
                             len(cached), os.path.basename(path), age_h)
                except Exception as e:
                    log.warning("  cache unreadable (%s), refetching", e)
                stale = [f for f in SNAPSHOT_FIELDS if f not in cached.columns]
                if stale and not cached.empty:
                    log.info("  cache predates field(s) %s - refetching",
                             ", ".join(stale))
                    cached = pd.DataFrame()

        have = set()
        if not cached.empty and "market_cap_local" in cached.columns:
            have = set(cached.loc[cached["market_cap_local"].notna(), "ticker"])
        todo = [t for t in tickers if t not in have]
        if not todo:
            log.info("  all %d tickers served from cache", len(tickers))
            return cached[cached["ticker"].isin(tickers)].reset_index(drop=True)

        # The limit is global (it rejects the crumb request), so probe once
        # single-threaded before opening the pool.
        try:
            yf.Ticker(todo[0]).info
        except Exception as e:
            if "RateLimit" in type(e).__name__ or "Too Many Requests" in str(e):
                log.error("Yahoo is rate-limiting this IP (%s). Nothing was "
                          "fetched; wait a few minutes and re-run - the cache "
                          "keeps whatever has already arrived.", type(e).__name__)
                return out_schema(cached, tickers)
            log.debug("probe ticker %s failed non-fatally: %s", todo[0], e)

        def one(t: str) -> dict:
            rec = {"ticker": t}
            i = {}
            if self.cfg.request_delay:
                time.sleep(self.cfg.request_delay)
            for attempt in (0, 1):
                try:
                    i = yf.Ticker(t).info or {}
                    if i.get("marketCap") is not None or i.get("regularMarketPrice"):
                        break
                except Exception as e:
                    log.debug("%s attempt %d: %s", t, attempt, e)
                    if "RateLimit" in type(e).__name__:
                        return rec
                if attempt == 0:
                    time.sleep(0.5 + self.cfg.request_delay)
            if not i:
                return rec
            price = i.get("regularMarketPrice") or i.get("currentPrice")
            mcap, src = i.get("marketCap"), "info"
            if mcap is None and price:
                # The Allianz case. fast_info is price x Yahoo's share-count
                # history; it is a separate request, so only made when needed.
                try:
                    mcap = yf.Ticker(t).fast_info.get("marketCap")
                    src = "fast_info" if mcap else ""
                except Exception as e:
                    log.debug("%s fast_info: %s", t, e)
                    src = ""
            rec.update({
                "yf_name": i.get("longName") or i.get("shortName"),
                "currency": i.get("currency"),
                "market_cap_local": mcap,
                "mcap_source": src if mcap else "",
                "close_local": price,
                "trailing_pe": i.get("trailingPE"),
                "price_to_book": i.get("priceToBook"),
                "ev_to_ebitda": i.get("enterpriseToEbitda"),
                # EPS and BPS stay as Yahoo gives them: ROE is their ratio,
                # unit-free and consistent with the P/E and P/B screened on.
                "trailing_eps": i.get("trailingEps"),
                "book_value_ps": i.get("bookValue"),
                # Percent (3.09 = 3.09%), the same scale as the 2% floor.
                "div_yield": i.get("dividendYield"),
                "yf_sector": i.get("sector"),
                "yf_industry": i.get("industry"),
                "yf_country": i.get("country"),
                "quote_type": i.get("quoteType"),
                "yf_shares": i.get("impliedSharesOutstanding") or i.get("sharesOutstanding"),
                "fin_ccy": i.get("financialCurrency"),
            })
            return rec

        log.info("  fetching %d tickers (%d already cached)", len(todo), len(have))
        rows: list[dict] = []
        with cf.ThreadPoolExecutor(max_workers=self.cfg.max_workers) as ex:
            for n, rec in enumerate(ex.map(one, todo), 1):
                rows.append(rec)
                if n % 50 == 0:
                    log.info("  fetched %d/%d", n, len(todo))

        fresh = pd.DataFrame(rows)
        parts = [d for d in (cached, fresh) if not d.empty]
        out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        out = out.reindex(columns=["ticker"] + list(SNAPSHOT_FIELDS))
        # Priced rows first so a cached hit beats a fresh throttled blank.
        out = out.sort_values("market_cap_local", na_position="last")
        out = out.drop_duplicates(subset=["ticker"], keep="first")

        try:
            os.makedirs(self.cfg.cache_dir, exist_ok=True)
            out.to_csv(self._cache_path(asof), index=False, encoding="utf-8")
        except Exception as e:
            log.warning("  could not write cache: %s", e)

        out = out[out["ticker"].isin(tickers)].reset_index(drop=True)
        got = int(out["market_cap_local"].notna().sum())
        fb = int((out["mcap_source"] == "fast_info").sum())
        log.info("  %d/%d tickers have a market cap (%d via fast_info)",
                 got, len(tickers), fb)
        return out

    # -- liquidity ---------------------------------------------------------
    def price_panel(self, tickers: list[str], days: int, chunk: int = 80) -> pd.DataFrame:
        """Daily close and volume for every ticker at once.

        yf.download is a different, batched endpoint from .info and does not
        share its throttle in practice (Japan measured ~20 requests for 661
        names). So the liquidity gate costs a handful of requests and still
        runs before any per-ticker statement work.
        """
        import yfinance as yf
        period = f"{max(int(days * 1.6), 30)}d"
        frames = []
        for i in range(0, len(tickers), chunk):
            part = tickers[i:i + chunk]
            try:
                px = yf.download(part, period=period, interval="1d", progress=False,
                                 auto_adjust=False, actions=False, threads=True,
                                 group_by="column")
            except Exception as e:                                # noqa: BLE001
                log.warning("price chunk %d failed: %s", i // chunk, e)
                continue
            if px is not None and not px.empty:
                frames.append(px)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1)

    @staticmethod
    def average_daily_value(panel: pd.DataFrame, tickers: list[str],
                            days: int) -> pd.DataFrame:
        """Median daily traded value in EUR over the last `days` sessions.

        Median, not mean: one index-rebalance day can be twenty times a name's
        normal volume. XETRA ONLY - the .DE line's volume does not include
        Frankfurt floor, Tradegate or off-exchange prints, so this is a lower
        bound on what actually trades. For the size of names that clear the
        cap floor Xetra is the main venue, but a name near the ADV line may be
        more tradeable than this says; --skip-liquidity removes the gate.
        """
        if panel.empty:
            return pd.DataFrame(columns=["ticker", "adv_local", "adv_sessions"])
        try:
            close, vol = panel["Close"], panel["Volume"]
        except KeyError:
            return pd.DataFrame(columns=["ticker", "adv_local", "adv_sessions"])
        if isinstance(close, pd.Series):                      # single ticker
            close, vol = close.to_frame(tickers[0]), vol.to_frame(tickers[0])
        rows = []
        for t in tickers:
            if t not in close.columns or t not in vol.columns:
                continue
            c = close[t]
            v = vol[t]
            if isinstance(c, pd.DataFrame):                   # duplicate column
                c, v = c.iloc[:, 0], v.iloc[:, 0]
            tv = (pd.to_numeric(c, errors="coerce")
                  * pd.to_numeric(v, errors="coerce")).dropna().tail(days)
            if tv.empty:
                continue
            rows.append({"ticker": t, "adv_local": float(tv.median()),
                         "adv_sessions": int(len(tv))})
        return pd.DataFrame(rows)

    # -- FX ----------------------------------------------------------------
    def eur_to_usd(self) -> float:
        """USD per EUR - the UK's direction (USD per unit of local currency,
        multiplied), not Korea's KRW-per-USD inverse. The name says which way
        round it is so a copied line cannot flip it silently.

        Frankfurter is the ECB's own reference rate, which for the euro is the
        natural source rather than a fallback; Yahoo is tried first only for
        consistency with the other builds.
        """
        import yfinance as yf
        try:
            h = yf.Ticker("EURUSD=X").history(period="5d")
            if not h.empty:
                rate = float(h["Close"].iloc[-1])
                if 0.8 < rate < 1.6:
                    return rate
        except Exception as e:
            log.info("FX via Yahoo failed (%s), trying Frankfurter", e)
        try:
            r = self.s.get("https://api.frankfurter.app/latest?from=EUR&to=USD",
                           timeout=20)
            rate = float(r.json()["rates"]["USD"])
            if 0.8 < rate < 1.6:
                log.info("FX from Frankfurter (ECB reference)")
                return rate
        except Exception as e:
            log.warning("FX via Frankfurter failed: %s", e)
        log.warning("FX: both live sources failed, using the hardcoded 1.15. "
                    "The USD size gate is only as good as this number.")
        return 1.15


# ---------------------------------------------------------------------------
# Filed statements: three-year history and the own-history benchmark
# ---------------------------------------------------------------------------
# Ported from the UK build unchanged in logic. The worked examples in the
# comments below (NatWest, Reckitt, Shell, Craneware) were measured THERE; the
# rules they justify are Yahoo's, not London's, so they hold for .DE lines too.
# The German cases that exercise them are in test_germany.py.
# Row names Yahoo uses in its annual statements, first match wins. Net income
# is taken to COMMON shareholders where Yahoo separates it: NatWest's differs
# by ~6% because of AT1 coupons, and a P/E is a price for the common equity.
IS_ROWS = {
    "rev": ("Total Revenue", "Operating Revenue"),
    "op": ("Operating Income", "Total Operating Income As Reported"),
    "ebitda": ("EBITDA", "Normalized EBITDA"),
    "np": ("Net Income Common Stockholders", "Net Income"),
    # Yahoo's "normalized" lines strip exactly its Total Unusual Items row. The
    # 3-year growth history stays on the REPORTED rows above, as Korea's does
    # - that is what happened. The own-history VALUATION uses these instead,
    # because a one-off is not a change in what the business is worth: Reckitt
    # sold Essential Home in 2025, reported EBITDA jumped 4,760 vs 3,966
    # normalized and net income 3,182 vs 2,535, and on reported figures it
    # passed the history screen on P/E and EV/EBITDA while its P/B - which a
    # disposal gain does not move - sat only 15% below its history. For an
    # ordinary year the two lines agree to within a few percent.
    "np_norm": ("Normalized Income",),
    "ebitda_norm": ("Normalized EBITDA",),
}
BS_ROWS = {
    "equity": ("Common Stock Equity", "Stockholders Equity"),
    "shares": ("Ordinary Shares Number", "Share Issued"),
    "debt": ("Total Debt",),
    "cash": ("Cash And Cash Equivalents",
             "Cash Cash Equivalents And Short Term Investments"),
    "mi": ("Minority Interest",),
}

# Quote currencies Yahoo reports in minor units, and the major unit each is.
MINOR_CCY = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0),
             "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}

# Consecutive filed share counts outside this band are treated as a corporate
# action (consolidation, split, rights issue) rather than buybacks. Shell's
# heavy buybacks move ~7% a year, so the band is wide enough to leave them
# alone. It cannot catch small consolidations; it catches the ones that would
# otherwise manufacture a 30%+ "discount" out of a unit change.
SHARE_BREAK_BAND = (0.67, 1.5)
# Today's price x latest filed shares must land near today's market cap. If it
# does not, the price series and the share count are on different bases and
# every historical market value built from them is wrong by the same factor.
NOW_BASIS_BAND = (0.6, 1.6)
# A latest filing older than this is not the latest filing - Yahoo has missed
# one. "Today" is then today's price over earnings from two years ago, while
# yfinance's own twelve-month figure knows better: Craneware read 52.6x on
# the same basis against 28.6x trailing, and passed the history screen on that
# gap. 13 of 251 survivors (Sept 2026) had a newest filed year of 2024. Eighteen
# months leaves room for a late filer without admitting a skipped year.
MAX_FILING_AGE_DAYS = 548

# The statements cache stores DERIVED records, not raw frames (seven years of
# daily prices per name would be ~40x larger). So a change to
# build_statement_record does not reach names already cached - bump this and
# the next run refetches instead of serving numbers the new code would not
# produce.
STATEMENTS_CACHE_VERSION = 3


def cagr(values: list) -> float:
    """Compound annual growth across the span the values actually cover.

    Identical to the Korea build: undefined when the base is zero or negative.
    A company that lost money three years ago has no meaningful growth RATE,
    and inventing one is worse than reporting nothing. The yearly figures
    always ship alongside, so nothing is hidden by this.
    """
    vals = [v for v in values if v is not None and np.isfinite(v)]
    if len(vals) < 2 or vals[0] <= 0 or vals[-1] <= 0:
        return np.nan
    return (vals[-1] / vals[0]) ** (1.0 / (len(vals) - 1)) - 1.0


def major_ccy(ccy) -> tuple[str, float]:
    """(major currency, divisor) for a Yahoo quote currency."""
    c = str(ccy or "")
    return MINOR_CCY.get(c, (c, 1.0))


def _row(df: pd.DataFrame, names) -> pd.Series:
    """First matching statement row, indexed by fiscal year-end, oldest first."""
    if df is None or df.empty:
        return pd.Series(dtype=float)
    for n in names:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors="coerce")
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            return s.sort_index()
    return pd.Series(dtype=float)


def _at(series: pd.Series, when: pd.Timestamp, tolerance_days: int = 10) -> float:
    """Last value on or before `when`, if it is within `tolerance_days`. A
    fiscal year-end that falls in a data gap is missing, not the nearest price
    from months earlier."""
    if series is None or series.empty:
        return np.nan
    s = series.loc[:when].dropna()
    if s.empty or (when - s.index[-1]).days > tolerance_days:
        return np.nan
    return float(s.iloc[-1])


def _num(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return np.nan
    return f if np.isfinite(f) else np.nan


def build_statement_record(inc: pd.DataFrame, bs: pd.DataFrame,
                           px: pd.Series, fx: pd.Series | None,
                           quote_ccy: str, fin_ccy: str,
                           close_now: float, mcap_now: float,
                           fin_years: int = 3, max_hist: int = 5,
                           asof: str | pd.Timestamp | None = None) -> dict:
    """Everything the two statement-based features need, from raw frames.

    Pure: no network, so test_germany.py can exercise every trap offline. `px` is
    the daily close in QUOTE units (euros for every .DE line); `fx` converts the
    quote's MAJOR currency into the reporting currency (None when they are the
    same); `close_now` and `mcap_now` are in the quote's major currency.

    Every multiple is a market value over a reported total, never a price over
    a per-share figure. Yahoo's per-share rows are in reporting-currency units
    against a price in the quote currency (Qiagen: dollars against euros), and
    its EPS row is frequently rounded to zero; totals over totals sidestep both.
    On a Stamm/Vorzug issuer the filed share count is both classes, which
    matches Yahoo's market cap (see the module docstring).

    Three-year history: the last `fin_years` filed years, oldest first, in
    millions of the REPORTING currency. Deliberately not converted - a growth
    rate should describe the business, not the euro against the dollar.

    Own history: for each filed year, that year-end market value over that
    year's filed figures, converted into the reporting currency at that
    year-end rate. Plus the latest filing's components, so the screen can put
    TODAY's market value over them on exactly the same definitions - see
    de_filters.apply_history_screen for why that matters.
    """
    rec: dict = {"fin_ccy": fin_ccy or ""}

    rows = {k: _row(inc, v) for k, v in IS_ROWS.items()}
    rows.update({k: _row(bs, v) for k, v in BS_ROWS.items()})

    # A fiscal year counts as filed when it has revenue or net income. Yahoo
    # pads the frame with an extra, entirely empty oldest column (2021 in
    # every UK name checked, and in German ones too), and that must not be read
    # as a zero year.
    dates = sorted(set(rows["rev"].dropna().index) | set(rows["np"].dropna().index))
    if not dates:
        rec["hist_note"] = "no filed years"
        return rec

    # ---- three-year history ---------------------------------------------
    fy = dates[-fin_years:]
    rec["fin_years"] = ",".join(d.strftime("%Y") for d in fy)
    rec["fin_n"] = len(fy)
    for key in ("rev", "op", "ebitda", "np"):
        series = [_num(rows[key].get(d)) / 1e6 for d in fy]
        for i, v in enumerate(series, 1):
            rec[f"{key}_y{i}"] = round(v, 1) if np.isfinite(v) else np.nan
        rec[f"{key}_cagr"] = cagr(series)

    # ---- own history ------------------------------------------------------
    hy = dates[-max_hist:]
    rec["hist_years"] = ",".join(d.strftime("%Y") for d in hy)
    q_major, q_div = major_ccy(quote_ccy)
    same_ccy = bool(fin_ccy) and fin_ccy == q_major
    # No reporting currency is not the same as euros. Assuming it would
    # treat a dollar reporter's accounts as pounds and scale every historical
    # multiple by the exchange rate - a fake discount or premium of 20-35%.
    # Missing means no benchmark (invariant 2), never a guess.
    if not fin_ccy:
        rec["hist_note"] = "no reporting currency"

    def fx_at(when):
        if same_ccy:
            return 1.0
        return _at(fx, when) if fx is not None else np.nan

    shares = [_num(rows["shares"].get(d)) for d in hy]
    lf = hy[-1]

    # Earnings basis for the VALUATION: normalized where Yahoo has it for
    # every year in the window, reported otherwise - never a mix within one
    # company's series, because a history that switches definition part-way
    # compares a year with itself on two different measures (see IS_ROWS).
    def pick(norm_key, rep_key):
        n = rows[norm_key]
        if all(np.isfinite(_num(n.get(d))) for d in hy):
            return n, "normalized"
        return rows[rep_key], "reported"
    ni_row, ni_basis = pick("np_norm", "np")
    eb_row, _ = pick("ebitda_norm", "ebitda")
    rec["hist_earnings"] = ni_basis

    # Guard 0: a stale latest filing. Checked first, because every other
    # number in the record would be struck against it.
    if asof is not None and "hist_note" not in rec:
        age = (pd.Timestamp(asof).normalize() - lf).days
        if age > MAX_FILING_AGE_DAYS:
            rec["hist_note"] = "stale filings"

    # Guard 1: a share count that jumps between filings is a corporate action.
    # The price series is split-adjusted while filed counts are not
    # necessarily, so a market value built across the break is wrong by the
    # split ratio - which the screen would read as a huge discount.
    valid = [s for s in shares if np.isfinite(s) and s > 0]
    for a, b in zip(valid, valid[1:]):
        if not (SHARE_BREAK_BAND[0] <= b / a <= SHARE_BREAK_BAND[1]):
            rec["hist_note"] = "share-count break"
            break

    # Guard 2: today's price x latest filed shares against today's market cap.
    s_lf = _num(rows["shares"].get(lf))
    if "hist_note" not in rec and np.isfinite(s_lf) and s_lf > 0:
        mc = _num(mcap_now)
        ratio = (_num(close_now) * s_lf / mc) if mc else np.nan
        if not (np.isfinite(ratio) and NOW_BASIS_BAND[0] <= ratio <= NOW_BASIS_BAND[1]):
            rec["hist_note"] = "price/share basis mismatch"

    per, pbr, evx = [], [], []
    for d, sh in zip(hy, shares):
        p = _at(px, d) / q_div if px is not None else np.nan
        mv = p * sh * fx_at(d)        # year-end market value, reporting ccy
        ni, eq = _num(ni_row.get(d)), _num(rows["equity"].get(d))
        eb = _num(eb_row.get(d))
        debt, cash = _num(rows["debt"].get(d)), _num(rows["cash"].get(d))
        mi = _num(rows["mi"].get(d))
        mi = 0.0 if not np.isfinite(mi) else mi
        # Non-positive denominators become NaN here; bounds are applied by the
        # screen, which is also where a loss year drops out of the benchmark.
        per.append(mv / ni if np.isfinite(mv) and ni > 0 else np.nan)
        pbr.append(mv / eq if np.isfinite(mv) and eq > 0 else np.nan)
        ev = mv + debt - cash + mi
        evx.append(ev / eb if np.isfinite(ev) and eb > 0 else np.nan)

    if "hist_note" in rec:
        per = pbr = evx = []
    rec["hist_per"] = [round(v, 2) if np.isfinite(v) else None for v in per]
    rec["hist_pbr"] = [round(v, 3) if np.isfinite(v) else None for v in pbr]
    rec["hist_evx"] = [round(v, 2) if np.isfinite(v) else None for v in evx]

    # Latest filing's components, for today's multiples on the same basis.
    fx_last = np.nan
    if same_ccy:
        fx_last = 1.0
    elif fx is not None and not fx.dropna().empty:
        fx_last = float(fx.dropna().iloc[-1])
    rec.update({
        # On the same earnings basis as the history, or today's value would be
        # compared across two definitions - the thing this whole design avoids.
        "lf_ni": _num(ni_row.get(lf)), "lf_equity": _num(rows["equity"].get(lf)),
        "lf_ebitda": _num(eb_row.get(lf)), "lf_debt": _num(rows["debt"].get(lf)),
        "lf_cash": _num(rows["cash"].get(lf)), "lf_mi": _num(rows["mi"].get(lf)),
        # Today's quote-major -> reporting-currency rate, carried so the screen
        # does not have to look it up again.
        "fx_now": fx_last,
    })
    rec.setdefault("hist_note", "")
    return rec


def _json_safe(rec: dict) -> dict:
    out = {}
    for k, v in rec.items():
        if isinstance(v, (float, np.floating)):
            out[k] = float(v) if np.isfinite(float(v)) else None
        elif isinstance(v, np.integer):
            out[k] = int(v)
        else:
            out[k] = v
    return out


def fetch_statements(snap: pd.DataFrame, cfg: ScreenConfig,
                     asof: str = "") -> pd.DataFrame:
    """Filed statements for the size-gated survivors. Invariant 7: this is the
    slowest work in the run, so it only ever sees names that already cleared
    every cheap filter.

    Three Yahoo calls per ticker (income statement, balance sheet, seven years
    of daily closes) plus one FX history per foreign reporting currency, paced
    like snapshot() and cached per session date for the same reason: a
    throttled statement call returns an empty frame, not an error, and an
    empty frame reads as "no history" - which quietly removes a name from the
    third screen instead of failing loudly.
    """
    import yfinance as yf

    tickers = snap["ticker"].tolist()
    path = os.path.join(cfg.cache_dir,
                        f"statements_v{STATEMENTS_CACHE_VERSION}_{asof or 'latest'}.json")
    cached: dict = {}
    if os.path.exists(path):
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if age_h <= cfg.cache_ttl_hours:
            try:
                with open(path, encoding="utf-8") as fh:
                    cached = json.load(fh)
            except Exception as e:
                log.warning("  statements cache unreadable (%s), refetching", e)

    # FX: one history per reporting currency that differs from the quote's.
    pairs = set()
    for _, r in snap.iterrows():
        q_major, _ = major_ccy(r.get("currency"))
        f = r.get("fin_ccy")
        if isinstance(f, str) and f and f != q_major:
            pairs.add((q_major, f))
    fxs: dict = {}
    for q, f in sorted(pairs):
        try:
            h = yf.Ticker(f"{q}{f}=X").history(period="7y", interval="1d")
            s = h["Close"].copy()
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            fxs[(q, f)] = s
            log.info("  FX history %s->%s: %d days", q, f, len(s))
        except Exception as e:
            log.warning("  FX history %s->%s failed: %s - those reporters get "
                        "no own-history benchmark", q, f, e)

    todo = [t for t in tickers if t not in cached]
    rows = {t: r for t, r in snap.set_index("ticker").iterrows()}

    def one(t: str):
        if cfg.request_delay:
            time.sleep(cfg.request_delay)
        r = rows[t]
        try:
            tk = yf.Ticker(t)
            inc, bs = tk.income_stmt, tk.balance_sheet
            h = tk.history(period="7y", interval="1d", auto_adjust=False)
        except Exception as e:
            log.debug("%s statements: %s", t, e)
            return t, None
        if inc is None or inc.empty:
            return t, None
        px = pd.Series(dtype=float)
        if h is not None and not h.empty:
            px = h["Close"].copy()
            px.index = pd.to_datetime(px.index).tz_localize(None).normalize()
        q_major, _ = major_ccy(r.get("currency"))
        f = r.get("fin_ccy") if isinstance(r.get("fin_ccy"), str) else ""
        rec = build_statement_record(
            inc, bs, px, fxs.get((q_major, f)), r.get("currency"), f,
            _num(r.get("close_local")), _num(r.get("market_cap_local")),
            asof=asof or None)
        return t, _json_safe(rec)

    if todo:
        log.info("  statements: fetching %d tickers (%d cached)", len(todo),
                 len(tickers) - len(todo))
        with cf.ThreadPoolExecutor(max_workers=cfg.max_workers) as ex:
            for n, (t, rec) in enumerate(ex.map(one, todo), 1):
                if rec is not None:
                    cached[t] = rec
                if n % 50 == 0:
                    log.info("  statements %d/%d", n, len(todo))
        try:
            os.makedirs(cfg.cache_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(cached, fh)
        except Exception as e:
            log.warning("  could not write statements cache: %s", e)

    got = [t for t in tickers if t in cached]
    log.info("  statements for %d/%d survivors", len(got), len(tickers))
    if len(got) < 0.8 * len(tickers):
        log.warning("  only %.0f%% have statements - Yahoo is probably "
                    "throttling. The two statement features will be thin; "
                    "re-run to fill the gaps from cache.",
                    100.0 * len(got) / max(len(tickers), 1))
    if not got:
        return pd.DataFrame(columns=["ticker"])
    out = pd.DataFrame([{"ticker": t, **cached[t]} for t in got])
    for k in ("hist_per", "hist_pbr", "hist_evx"):
        if k in out.columns:
            out[k] = out[k].map(lambda v: v if isinstance(v, list) else [])
    # fin_ccy also comes from the snapshot, and the snapshot's is the one kept.
    return out.drop(columns=["fin_ccy"], errors="ignore")
