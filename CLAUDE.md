# CLAUDE.md — Germany (Deutsche Börse) Valuation Screener

Screens every Deutsche Börse equity segment (Prime Standard, General Standard,
Scale, Basic Board) for stocks ≥20% below industry-peer median on P/E, P/B,
EV/EBITDA, plus two independent screens: cheap outright, and cheap against the
company's own filed history. Gates: market cap ≥ USD 600M, median daily traded
value ≥ USD 4M on Xetra. Fourth market after Korea, the UK and Japan;
`screener.py` is the same maths.

## Run order

```bash
python test_germany.py    # offline logic check — must pass, no network needed
python check_setup.py     # eight live checks, each naming what it protects
python main_de.py -v      # full run, ~4 min cold, under a minute warm

# each run rewrites de_dashboard.html in place — open it, re-run, reload
python serve.py           # 127.0.0.1:8765, opens a browser, Refresh button works
python serve.py --min-roe 8       # extra args go to the run

python main_de.py --skip-liquidity    # size alone gates
python main_de.py --include-property  # keep listed landlords
python main_de.py --both-lines        # keep Stamm AND Vorzug (double-counts)
```

## Files

| File | Role |
|---|---|
| `screener.py` | Core engine, ported from the UK/Korea build. Financials by exchange sector, as in Japan. Do not edit the maths here first. |
| `config_de.py` | Thresholds, metric bounds, segment map, subsector typo fixes, financial / PE / property subsectors, Vorzug and issuer detection. |
| `providers_de.py` | Deutsche Börse roster, Yahoo snapshot with fast_info fallback, ISIN symbol resolution, batched liquidity, FX, filed statements (UK's `build_statement_record`, unchanged). |
| `de_filters.py` | German hygiene, one line per issuer, ROE gate, absolute screen, own-history screen, the TTM-vs-filed flag. |
| `main_de.py` | CLI. Orders the gates so per-ticker work runs last. |
| `dashboard.py` | Generated — do not edit. Run `sync_dashboard_from_korea.py`. |
| `sync_dashboard_from_korea.py` | Korea's `dashboard.py` + 68 German substitutions. Every one must match exactly once. |
| `serve.py` | Local server behind the dashboard's Refresh button. |
| `build_site.py` | Assembles `site/` for GitHub Pages (noindex, no Refresh button). |
| `dashboard.cmd` | Double-click launcher. |
| `check_setup.py` | Pre-flight diagnostic. |
| `test_germany.py` | Offline tests with planted traps. Keep green. |

## Invariants — do not remove without understanding why

1. **One line per issuer, and it is the LIQUID line, not the Vorzug-dropped
   line.** This is Korea's 우선주 invariant with the sign reversed. In Seoul the
   preferred is always the illiquid, discounted line and dropping it is the
   fix. In Frankfurt the Vorzug is often where the volume is — VW, Henkel,
   Sartorius, Fuchs, KSB and Drägerwerk all keep the Vorzug — and the gap
   between lines runs both ways (Sixt Vz ~19% *below* its Stamm, Sartorius Vz
   ~24% *above*). Two lines of one company in one screen are wrong either way:
   same EPS, same book, so the cheaper line is scored against a cohort that
   holds its own sibling. `keep_one_line_per_issuer` keeps the higher median
   traded value.

   **Not the index line.** The exchange file marks VW's, Sartorius's and
   Fuchs's *Stamm* as the index member while their Vorzug trades 3–30x more.
   Keying on the index flag would keep the line nobody trades.

   Issuers are grouped by **ISIN prefix AND name** (`issuer_key`). Neither
   alone works: ATOSS and SYZYGY share `DE0005104`, and the name must have
   punctuation removed *before* class words — "DRAEGERWERK ST.A.O.N." vs
   "DRAEGERWERK VZO O.N." kept both Drägerwerk lines in the first full run.
   8 dual-class issuers on the 2026-09-01 file.

2. **P/E, P/B of 0 or negative are missing, never cheap.** `METRIC_BOUNDS`.
   Yahoo hands out negatives freely here (Porsche SE EV/EBITDA −217).

3. **Peer groups are the exchange's subsector, falling back to its sector.**
   Not industry × board: 149 of 169 survivors are Prime Standard, and DAX /
   MDAX / SDAX medians are close (P/B 2.17 / 1.83 / 1.99), so neither board
   nor index tier earns a place in the key. With subsector → sector, 143–149
   of 169 names get a benchmark per metric — against about a third in the UK
   build, which had to use Yahoo's industry.

4. **Peer counts exclude the stock itself**; benchmark is a winsorized median.

5. **`avg_discount` averages every metric with data**, including failing ones.

6. **EV/EBITDA is suppressed for financials, by exchange sector** (Banks,
   Insurance, Financial Services) — **except Real Estate**, which the exchange
   files under Financial Services but whose enterprise value is meaningful.

7. **Slow calls run last.** The roster is one request; `.info` runs for all
   ~460 lines because it returns market cap and multiples together (the UK
   build's reading of this invariant); the batched price download and three
   statement calls per name run only past the size gate.

8. **The ROE floor runs after scoring, never before.** Missing ROE fails.

9. **Three independent screens**, unioned into `passes_any`.

10. **Financials clear the absolute screen on P/B + ROE** (`abs_via_carveout`).
    On the reference run that is the only absolute pass (Deutsche Bank).

11. **A missing market cap is repaired, not read as small.** Yahoo's `.info`
    returned every multiple and no `marketCap` for Allianz and 1&1 — 18 lines
    on 2026-10-02, 10 of the 85 survivors. `fast_info` recomputes it;
    `mcap_source` records which path each row took.

12. **A symbol Yahoo does not know is resolved by ISIN, not dropped.**
    Schaeffler (MDAX) is SHA on the exchange and SHA0 on Yahoo; Uniper is UN01
    and UN0; Einhell's Vorzug EIN3 and EIN. 26 lines resolved per run. Hits on
    a foreign exchange only (Vulcan on the ASX) are left unpriced on purpose.
    Frankfurt-only lines take `.F` — they have no `.DE` quote at all.

13. **The subsector typos are folded** (`SUBSECTOR_FIXES`): the file spells
    Pharmaceuticals two ways, Health Care two ways, Retail, Internet two ways.
    Unfolded, each spelling is its own thin cohort.

## Excluded by default, and why

| class | rule | 2026-10-02 |
|---|---|---|
| listed PE / holding vehicles | subsector *Private Equity & Venture Capital* | 15 |
| listed landlords | subsector *Real Estate* | 25 |
| REITs | name / Yahoo industry | 5 |
| foreign secondary lines | domicile ≠ Germany and no German index | 28 |
| second share class | `keep_one_line_per_issuer` | 7 |

**Landlords** are the judgement call. IAS 40 runs revaluations through
earnings: LEG at P/E 3.0, Vonovia 3.8, Grand City 3.1, Deutsche Wohnen 4.1.
That is the appraiser's year, not a rental business. Korea and the UK exclude
REITs for exactly this reason and German landlords are REITs in all but tax
status. `--include-property` keeps them, with EV/EBITDA left on.

**Foreign secondaries**: UniCredit, Haier's D-shares (which trade at a standing
discount to its A-shares — market access, not value), BB Biotech. Index
membership is the exchange's own judgement that a foreign issuer's main market
is here, so Airbus, Qiagen and Redcare stay.

## The liquidity gate binds hard, and it is Xetra only

175 names clear USD 600m; **85 clear USD 4m a day**. At USD 1m / 2m / 3m the
count is 123 / 105 / 91. The ADV is the 60-session median of close × volume on
the `.DE` line — **Xetra only**, not Frankfurt floor, Tradegate or off-book —
so it is a lower bound on what trades.

The gate is kept at the cross-market USD 4m and applied (as Japan does, since
this ADV is a real median, unlike the UK's proxy), because what it removes is
mostly correct to remove: near-zero free float (EnBW, Uniper, Hapag-Lloyd,
DMG Mori, HHLA, MVV) whose prices are not a market's verdict. The cost is MDAX
and SDAX names that trade just under the line on Xetra — Krones, United
Internet, Fuchs, Sixt, Fielmann. **`--min-adv 1e6` keeps the free-float hygiene
and admits them**: measured on the same close, 123 survivors and 26 passes
(18 relative, 2 absolute, 8 history) against 85 and 16.

## The TTM-vs-filed flag

The peer and absolute screens use Yahoo's trailing multiples, as every market
does. Yahoo's trailing twelve months is the sum of the last four quarters,
one-offs included. **K+S tops the peer screen on accounting**: quarterly net
income of +571m (Q4 2025) and +646m (Q2 2026), against a filed 2025 loss of
€1.08bn (−1.14bn normalized), gives TTM +€1.06bn, P/E 2.6, ROE 20%. Quarterly
EBITDA of 748m against ~640m for all of 2025 is the tell. It is not a sign
error — checked against the quarterlies.

`flag_ttm_vs_filed` marks *TTM profit against a normalized filed loss* — K+S
and Salzgitter on the reference run — and the dashboard tags them `ttm vs fy`.
It does not change a verdict. A ratio test was tried and rejected: it also
flags Siemens Energy and RWE, which are genuine turnarounds.

## Calibration, measured rather than inherited

169 names past the size gate (property excluded), 2026-10-02:

| | p10 | p25 | median | p75 | p90 |
|---|---|---|---|---|---|
| P/E | 9.82 | 13.54 | **18.64** | 28.73 | 52.95 |
| P/B | 0.81 | 1.25 | **2.08** | 4.17 | 6.58 |
| EV/EBITDA | 6.38 | 7.87 | **11.24** | 16.47 | 25.29 |
| ROE % | −7.50 | 3.63 | **9.01** | 17.47 | 25.28 |
| Yield % | 0.00 | 0.37 | **1.71** | 2.94 | 4.55 |

Korea's absolute levels survive: **P/B < 1 passes 16.0%, EV/EBITDA < 8 passes
23.7%** — both near the cheapest quartile, roughly equally strict. Unlike the
UK they did not need re-cutting.

Which test binds (size-gated, property removed): 13 names are below book AND
below 8x EBITDA; ROE ≥ 5% leaves 7, fair P/B 3, the dividend floor 2. No test
is a no-op. **Cost of equity stays 10%**: at 9% the counts are identical, at 8%
one more name passes. Germany has no Ito-Review convention to anchor a lower
number to.

Germany's below-book names are mostly the low-ROE kind — correctly priced, not
discounted. A thin absolute screen is the finding, not a bug.

## Results, reference run (2026-10-02 close)

```
458 lines on four segments
  450 priced (26 via ISIN resolution, 18 market caps via fast_info)
  385 after hygiene (−15 PE vehicles, −25 landlords, −5 REITs, −28 foreign)
  175 cleared USD 600m
   85 cleared USD 4m/day on Xetra (−7 second share-class lines)
    8 cheap vs peers (all clear ROE ≥ 5%)
    1 cheap outright (Deutsche Bank, via the financials carve-out)
    7 cheap vs own history (none found by another screen)
   16 passing at least one screen
```

Browser verdict at default thresholds equals Python's: 16 / 8 / 1 / 7, and
every per-test count (P/B < 1 14, EV < 8 16, fair 10, yield 34, ROE 66).

## Known gaps (ranked by value of fixing)

1. **ADV is Xetra only.** See above. A multi-venue volume source would make
   the gate honest rather than conservative.
2. **Listed subsidiaries are not identified.** Traton (VW ~90%), Porsche AG,
   Daimler Truck, Siemens Healthineers, Talanx — a majority parent changes
   what a minority discount means. Japan's 親子上場 gap, same cause: no free
   shareholder data. Domination agreements (Beherrschungsvertrag), where
   minorities get a fixed compensation and the P/E is meaningless, are the
   extreme case; index free-float rules keep most out, the liquidity gate most
   of the rest.
3. **The roster is monthly.** The workbook is stamped with its as-at date
   (2026-09-01 here); `main_de.py` warns past 75 days. New listings since the
   stamp are missing.
4. **Four filed years** for the history screen, as in the UK.
5. Trailing multiples only; no forward estimates.

## Likely first failures

- **Yahoo throttles silently.** `.info` returns dicts with fields missing.
  The snapshot probes once, caches per session date, and `main_de.py` refuses
  to screen if under half the Xetra lines priced. Recovery is waiting.
- **The workbook moves.** `_roster_url()` reads the listed-companies page for
  the current blob link, falls back to the known URL, and finds the header row
  by its ISIN cell. `check_setup.py` asserts SAP, VOW3, ALV, DBK are present.
- **A blank market cap or renamed symbol** reads as "too small" unless
  repaired — invariants 11 and 12. Watch `priced_via_isin` and
  `mcap_via_fast_info` in the funnel; a sudden drop means a repair path broke.
- **The statements cache stores derived records** — bump
  `STATEMENTS_CACHE_VERSION` when `build_statement_record` changes.
- **pandas 3.x** — pinned `<3`.

## Keeping in step with Korea

```bash
python sync_dashboard_from_korea.py ../Korea/dashboard.py
```

Every substitution must match exactly once; misses exit 1. Then grep for
Korean text, KOSPI, PER/PBR and `ticker`, and confirm in a browser that the
page's verdict equals the Python funnel at default thresholds.

## Publishing

`.github/workflows/screen.yml` runs at 17:00 UTC on weekdays (after the Xetra
close in both CET and CEST), builds `site/`, and deploys to GitHub Pages, with
a retry after a pause for Yahoo throttling. **Not yet pushed or run in CI** —
Yahoo answers GitHub runners (the UK build confirmed it); Deutsche Börse's
workbook from a runner IP is unverified until the first run.

## Interpretation

Sort by `avg_discount`, then read `roe_pct` immediately, then look for tags.
`vz` means the vote sits elsewhere; `ttm vs fy` means read the P/E twice;
`holdco` means a portfolio discount may be structural. Low P/B with low ROE is
arithmetic, not opportunity — and that describes most of what trades below
book in Germany.

Research tool, not investment advice.
