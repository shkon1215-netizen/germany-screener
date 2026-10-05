"""Configuration for the Germany (Deutsche Börse) valuation screener.

Fourth market after Korea, the UK and Japan. The quality floor, the discount
test and the history screen are the user's screening preferences and are
carried over unchanged. What is local:

  * universe hygiene - Germany's structural traps are dual-class lines
    (Stamm-/Vorzugsaktien) and listed private-equity holding vehicles, not
    Korea's preferreds or the UK's investment trusts
  * classification - Deutsche Börse's own sector/subsector, read off the
    exchange's listed-companies file, like Japan's 33業種
  * the absolute screen's valuation levels, re-measured on this universe -
    see the percentile table on ScreenConfig.abs_max_pbr
"""
from __future__ import annotations

import re
from dataclasses import dataclass

VALUATION_METRICS = ("trailing_pe", "price_to_book", "ev_to_ebitda")

# German convention is KGV / KBV, but the dashboard is read in English and the
# UK build already uses P/E and P/B - one set of labels for the two European
# pages.
METRIC_LABELS = {
    "trailing_pe": "P/E",
    "price_to_book": "P/B",
    "ev_to_ebitda": "EV/EBITDA",
}

# Same reasoning as Korea: a zero or negative multiple means "no earnings" or
# "negative equity", never "cheap". Yahoo hands out negatives on German lines
# freely - Porsche SE's EV/EBITDA comes back as -217, BayWa's P/B as -0.42.
METRIC_BOUNDS = {
    "trailing_pe": (1.0, 200.0),
    "price_to_book": (0.05, 30.0),
    "ev_to_ebitda": (0.5, 100.0),
}

# ---------------------------------------------------------------------------
# Deutsche Börse segments -> board label
# ---------------------------------------------------------------------------
# Prime and General Standard are the regulated market; Scale and the Basic
# Board are the open market (Freiverkehr). Every sheet of the listed-companies
# file maps to one board; a sheet not named here is not read.
SEGMENTS = {
    "Prime Standard": "PRIME",
    "General Standard": "GENERAL",
    "Scale": "SCALE",
    "Basic Board": "BASIC",
}
BOARDS = tuple(SEGMENTS.values())

# ---------------------------------------------------------------------------
# Classification hygiene
# ---------------------------------------------------------------------------
# The exchange's file is hand-maintained and it shows: the same subsector is
# spelled two or three ways across rows. Left alone, each spelling becomes its
# own peer cohort, which silently halves the cohorts it splits - two
# "Pharmaceuticals" names and two "Pharmatceuticals" names are four peers, not
# two pairs. Measured on the 2026-09-01 file.
SUBSECTOR_FIXES = {
    "Pharmatceuticals": "Pharmaceuticals",
    "HealthCare": "Health Care",
    "Retail,Internet": "Retail, Internet",
    "Industrial, Machinery": "Industrial Machinery",
}

# Financials, by the exchange's own sector, so invariant 6 is exact rather than
# a substring match on Yahoo's sector (which calls Deutsche Börse, Allianz and
# a PE holding all "Financial Services"). REAL ESTATE IS THE EXCEPTION: the
# exchange files Vonovia, LEG and TAG under "Financial Services" too, but a
# property company has a meaningful enterprise value - its debt is the
# business - so EV/EBITDA stays on for them.
FINANCIAL_SECTORS = ("Banks", "Insurance", "Financial Services")
NON_FINANCIAL_SUBSECTORS = ("Real Estate",)

# Listed private equity and holding vehicles. These are to Frankfurt what
# investment trusts are to London: a balance sheet of stakes in other
# companies, valued by the market at a standing discount to NAV that nothing
# forces closed. Their P/B is price-to-NAV and its long-run average is below
# one. The exchange names the class outright, so this is a classification
# lookup, not a heuristic.
INVESTMENT_COMPANY_SUBSECTORS = ("Private Equity & Venture Capital",)

# Listed landlords. German property companies account for their buildings at
# fair value under IAS 40, so every revaluation runs through the income
# statement and "earnings" are mostly the appraiser's opinion of the year.
# Measured 2026-10-04: LEG at P/E 3.0, Vonovia 3.8, Grand City 3.1, Deutsche
# Wohnen 4.1 - none of those is a rental business earning a third of its
# market value in a year. They are valued on NAV and FFO, which is exactly why
# Korea and the UK exclude REITs; Germany's landlords are REITs in all but tax
# status, so they are excluded by default (--include-property keeps them, and
# then config_de.is_financial still gives them EV/EBITDA).
PROPERTY_SUBSECTORS = ("Real Estate",)


@dataclass
class ScreenConfig:
    # --- Size / liquidity, specified in USD then converted at live FX ---
    min_market_cap_usd: float = 600_000_000
    min_adv_usd: float = 4_000_000
    adv_lookback_days: int = 60                 # trading days

    # --- Valuation test ---
    discount_threshold: float = 0.20
    metrics: tuple[str, ...] = VALUATION_METRICS
    min_metrics_passing: int = 2
    min_valid_metrics: int = 2

    # --- Quality floor ---
    min_roe_pct: float = 5.0
    roe_good_pct: float = 10.0

    # --- Absolute value screen ---
    abs_max_pbr: float = 1.0
    abs_max_ev_ebitda: float = 8.0
    abs_require_roe: bool = True
    abs_financials_pbr_only: bool = True
    abs_require_pbr_vs_roe: bool = True
    abs_cost_of_equity_pct: float = 10.0
    abs_min_div_yield: float = 2.0

    # --- Own-history screen ---
    # Same parameters as Korea and the UK. Yahoo holds four filed years for
    # German companies, like the UK, so hist_min_years = 3 means three of four.
    hist_min_discount: float = 0.30
    hist_min_metrics: int = 2       # of P/E, P/B, EV/EBITDA; financials have no EV/EBITDA
    hist_min_years: int = 3
    hist_require_roe: bool = True

    # --- Peer groups ---
    # industry = Deutsche Börse subsector, sector = Deutsche Börse sector.
    peer_keys: tuple[str, ...] = ("industry",)
    fallback_peer_keys: tuple[str, ...] = ("sector",)
    min_peers: int = 5
    winsor_pct: float = 0.05

    # --- Germany-specific universe hygiene ---
    one_line_per_company: bool = True       # Stamm + Vorzug of one issuer: keep one
    exclude_investment_companies: bool = True
    exclude_reits: bool = True
    exclude_property: bool = True           # IAS 40 landlords - see PROPERTY_SUBSECTORS
    flag_holdcos: bool = True
    exclude_holdcos: bool = False           # flagged by default, not dropped

    exclude_sectors: tuple[str, ...] = ()
    boards: tuple[str, ...] = BOARDS

    # --- Fetching ---
    # Yahoo's .info is the call that throttles, globally and silently - the
    # UK build's measured setting.
    max_workers: int = 2
    request_delay: float = 0.25
    cache_dir: str = ".de_cache"
    cache_ttl_hours: int = 20


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------
REIT_TOKENS = ("REIT",)
HOLDCO_TOKENS = ("HOLDING", "BETEILIGUNG", "HLDG")

# Vorzugsaktien. The exchange file writes them "VZO", "VZ", "Vz" or "-VZ-";
# the trailing-3 Xetra symbol (VOW3, HEN3, SRT3) is the other convention.
PREF_NAME = re.compile(r"(?:\bVZO?\b|-VZ-|\bVORZ)", re.IGNORECASE)


def norm_subsector(s: str) -> str:
    s = re.sub(r"\s+", " ", str(s or "").replace("\xa0", " ")).strip()
    return SUBSECTOR_FIXES.get(s, s)


def norm_sector(s: str) -> str:
    # "Financial services" and "Financial Services", "Basic resources" and
    # "Basic Resources", trailing spaces on "Industrial " - all in one file.
    s = re.sub(r"\s+", " ", str(s or "").replace("\xa0", " ")).strip()
    return " ".join(w if w in ("&",) else w[:1].upper() + w[1:] for w in s.split(" ")) \
        if s and s != "-" else ""


def is_financial(sector: str, subsector: str = "") -> bool:
    return (str(sector).strip() in FINANCIAL_SECTORS
            and str(subsector).strip() not in NON_FINANCIAL_SUBSECTORS)


def is_investment_company(subsector: str) -> bool:
    return str(subsector).strip() in INVESTMENT_COMPANY_SUBSECTORS


def is_property(subsector: str) -> bool:
    return str(subsector).strip() in PROPERTY_SUBSECTORS


def is_pref_name(name: str) -> bool:
    return bool(PREF_NAME.search(str(name)))


def is_reit(name: str) -> bool:
    n = str(name).upper()
    return any(t in n for t in REIT_TOKENS)


def is_holdco(name: str) -> bool:
    n = str(name).upper()
    return any(t in n for t in HOLDCO_TOKENS)


def issuer_key(isin: str, name: str) -> str:
    """Groups the listed lines of one issuer.

    Stamm and Vorzug of one company carry different ISINs (DE0007664005 VW St,
    DE0007664039 VW Vz) that share their first nine characters by convention -
    but so do unrelated companies: ATOSS (DE0005104400) and SYZYGY
    (DE0005104806). So the exchange's company name is the other half, and both
    must agree.

    The name is reduced to its first two words after punctuation becomes space
    and share-class words are dropped. The order matters: the exchange writes
    Drägerwerk's lines "DRAEGERWERK ST.A.O.N." and "DRAEGERWERK VZO O.N.", and
    matching class words before splitting on the dots left "A" on one and "O"
    on the other, which kept both lines of one company in the screen.
    """
    n = re.sub(r"[^A-Z0-9&+]", " ", str(name).upper())
    toks = [t for t in n.split() if t not in CLASS_WORDS][:2]
    return str(isin)[:9] + "|" + " ".join(toks)


# Share-class and register words in the exchange's company names: Stamm (ST),
# Vorzug (VZ, VZO), registered (NA, NAM, VNA), bearer (INH), restricted
# transfer (VINK, VV), no par value (O.N. -> O, N, ON), A-shares, euro par (EO).
CLASS_WORDS = {"ST", "VZ", "VZO", "PREF", "NA", "NAM", "VNA", "INH", "VINK", "VV",
               "O", "N", "ON", "A", "EO", "SP"}
