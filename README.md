# Germany (Deutsche Börse) Relative Valuation Screener

Finds German-listed stocks trading at a discount to their industry peers on
P/E, P/B and EV/EBITDA — and, independently, stocks that are cheap in absolute
terms, and stocks that are cheap against their own filed history. Each row also
carries three years of revenue, EBITDA and net profit.

**Defaults:** market cap ≥ USD 600M · median daily traded value ≥ USD 4M on
Xetra · ≥20% below peer median on ≥2 of 3 metrics · ROE ≥ 5%.

The fourth market after [Korea](https://github.com/shkon1215-netizen/korea-screener),
the UK and Japan. `screener.py` is the same maths; what is local is the data
layer and the share-class hygiene.

## Setup

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
python test_germany.py   # offline logic check, no network needed
python check_setup.py    # tests every live call individually
python main_de.py -v     # full run, ~4 minutes cold, under a minute warm
```

```bash
python main_de.py --skip-liquidity             # size alone gates
python main_de.py --min-adv 1e6                # a lower Xetra liquidity bar
python main_de.py --discount 0.15 --min-metrics 1
python main_de.py --include-property --all     # see why landlords are off
python main_de.py --fx 1.12                    # pin EUR/USD
```

Results cache to `.de_cache/` per session date, so re-runs are fast — and so a
throttled Yahoo cannot silently shrink the universe.

## The dashboard

Every run writes a self-contained `de_dashboard.html`. Open it directly, or run
`python serve.py` (on Windows, double-click `dashboard.cmd`) to make the
**Refresh** button work. Every threshold on the page is re-evaluated in the
browser; peer medians and sub-floor market caps cannot be, and the page says so.

## Where the data comes from

| | source |
|---|---|
| roster, sector, subsector, index | Deutsche Börse's own *Listed companies* workbook — every Prime Standard, General Standard, Scale and Basic Board line |
| multiples, EPS, book, yield | Yahoo `.info` on the Xetra line, `fast_info` when the market cap is blank |
| liquidity | one batched Yahoo price download, 60-session median traded value |
| filed statements | Yahoo annual income statement and balance sheet |

## German traps this handles

**Two share classes, one company.** VW, Henkel, Sartorius, Fuchs, KSB, Sixt,
Drägerwerk and BayWa list both a Stamm and a Vorzug line. Only one line per
issuer is screened — the one with more traded value — because both carry the
same earnings and book. Unlike Korea, the Vorzug is often the line kept.

**Listed private-equity and holding vehicles** (Deutsche Beteiligungs, Mutares,
MBB…) trade at a standing discount to NAV and are excluded, named by the
exchange's own subsector.

**Listed landlords** (Vonovia, LEG, TAG…) are excluded by default: IAS 40
revaluations flow through earnings, so their P/E is 3–4x on appraisal gains.

**Foreign secondary lines** (UniCredit, Haier's D-shares) are excluded; foreign
companies inside a German index (Airbus, Qiagen) stay.

**Yahoo's blank market caps and stale symbols.** Allianz comes back with no
market cap; Schaeffler, Uniper and Einhell are filed under different symbols.
Both are repaired (fast_info, ISIN lookup) and counted in the funnel.

**Trailing one-offs.** A `ttm vs fy` tag marks names profitable over the last
twelve months but loss-making in their last filed year — K+S's P/E of 2.6 is
two quarters of non-operating gains, not its business.

Research tool, not investment advice.
