# Logic file — Part C: the provider rate file (C6) and cost effectiveness (C7)

## C6 — One file at NPI × TIN × address × billing code
File: `data/stage6/provider_rate_file.xlsx`. Sheets: **Summary** (what the file is and how far to trust it, in 14 lines),
**Rate File** (deliverable), **Rate Variants** (every extracted rate), **Estimated Rates** (separate, not file rates), **Read Me**.
Built on the final C1–C5 outputs: the one CA in-network file that C1 found, used by the 453 PPO plans of C2; C4's one-rate-per-service
with Medicare benchmark, contract formula and rate_check; C5's addresses with tiers and measured accuracy.
Required columns come first: NPI, TIN, Address, Billing Code, Specialty, Rate. Then supporting columns: tier, place of service,
modifier, variant min/max, specialty source, network hint, evidence.

### D6.1 Which rate goes in the single Rate column
After C4, 619 of 6,642 NPI × TIN × code combinations still have more than one row. The **only** differences are the modifier
(e.g. 95 telehealth, 26 professional component, TC technical component) and the place of service (11 office, 22 outpatient
hospital, …). These are legitimately different prices, not noise.
- **Rule:** never a capitation row while a fee-for-service price exists; then the provider's **own component** (professional for a
  pair that bills mostly professional, facility for a hospital/ASC that bills mostly facility); then the row with **no modifier**
  (the standard, global service), then the one at **office (POS 11)**, else the most common place of service; if several remain,
  take their median. (C4 has already chosen one rate per service: the primary contract's price.) `rate_component`,
  `revenue_code`, `rate_source` and `n_contracts` are columns of the Rate File.
- **Why:** this is the like-for-like price contracting teams compare across providers. Mixing a telehealth or technical-component price
  into one number would mislead.
- **Result:** the chosen place of service is office 11 for 3,501 combinations (02 telehealth 436, 22 outpatient 399, …). There is no modifier
  for 4,063. 1,112 use modifier 95, because for those codes (e.g. psychotherapy) the provider is priced **only** with telehealth.
- **Nothing hidden:** `n_rate_variants`, `variant_rate_min`/`max` and the full **Rate Variants** sheet keep every price.

### D6.4 Trust score on every row (`trust_score`, `trust_band`, `trust_detail`)
One number (0–100) saying how far to trust a row's rate, built from five independent checks. The points are shown on
the row, so the score is never a black box:
rate_check OK **30** · every contract pricing it agrees **15** (else 5) · fits the provider's recovered contract formula
**20** (no formula 10, misfit 0) · PPO-network evidence CONFIRMED **20**, PRESUMED 12, MIXED 8, HMO_SIGNAL 0 · address
VERIFIED **15**, PROBABLE 10, UNVERIFIED 4. Bands: HIGH ≥ 80 (6,156 rows), MEDIUM 60–79 (3,559), LOW < 60 (1,228).
**Strict view** (Summary sheet): trust_band HIGH and PPO evidence CONFIRMED or PRESUMED = 6,041 rows (55.2%).
The Excel also has an **Estimated Rates** sheet (C4 step 9), kept apart from the real rates.

### D6.2 Which addresses
Every VERIFIED and PROBABLE address of the (NPI, TIN). If there is none, the single best UNVERIFIED address, or NONE.
So no rate is dropped for lack of an address, and the tier tells the user how far to trust it.
**Result (final, PPO network only, 2026-10-18):** 9,501 rows: VERIFIED 6,631 · PROBABLE 297 · UNVERIFIED 2,560 · NONE 13.
2,869 NPIs, 2,263 TINs, 2,947 addresses, 195 codes; professional 7,650 rows, facility 1,851 (hospital/ASC fees). Address accuracy measured by the
hold-out state-licence audit (Part B §3.6) is on every row: VERIFIED 97.5%, UNVERIFIED 86.0%.
Every extracted (NPI, TIN, code) is present. The file has fewer rows than in the first run (11,724) because an NPPES address
no longer counts as PROBABLE just because the NPPES record was updated recently (Part B §3.1): those pairs now carry their
single best address, labelled UNVERIFIED, instead of several weakly supported ones.

### D6.5 PPO network only: HMO-signal providers leave the Rate File
**Why:** the PS asks for the PPO network's rates. The one CA file is shared by PPO and HMO plans and carries no network field
(C2), so every provider is graded on evidence (Part A §4.1). 135 providers with rates are **HMO_SIGNAL**: 63 were **paid
out-of-network by the PPO plans themselves** (allowed-amounts file), 50 have a **capitation** contract (an HMO payment model),
29 carry an **IPA / Ambetter-HMO** group label.
**Rule:** their 1,442 rows move to the sheet **"Excluded - HMO signals"** (also `provider_rate_file_excluded_hmo.csv`), each
with `exclusion_reason`. MIXED (3 providers, 481 rows: a proven PPO sign, e.g. practising where Health Net has no HMO, plus an
HMO sign) stays in the Rate File, flagged. Rate Variants and Estimated Rates follow the same split; C7 scores PPO rows only.
**Result:** the Rate File carries no AMBETTER_HMO or IPA labels; 9,501 PPO-network rows remain.

### D6.3 Specialty on each row (every row has a HiLabs specialty)
**Problem found:** 3,330 rows (30%, 475 providers) were "Unmapped": 146 hospitals (HiLabs has no hospital specialty), clinics,
FQHCs, and taxonomies the HiLabs crosswalk doesn't list (anesthesiology, generic "Specialist").
**Rules, strongest first** (`specialty_source`): NPPES_PRIMARY / NPPES_SECONDARY (taxonomy in the HiLabs crosswalk) →
FACILITY_MAP (NUCC-verified facility taxonomy) → CODE_PROFILE (the provider's priced HiLabs codes; **organizations now use
their whole service mix, facility fees included**, because an organization's specialty is what it does, while individuals still
use professional fees only) → **CODE_DEFAULT**: a provider that still has no HiLabs specialty (mostly hospitals) gets, per row,
the HiLabs specialty that owns the billing code (highest SPEC_PROC_WEIGHT), e.g. a hospital's ER visit → Emergency Medicine.
`provider_taxonomy` keeps the provider's own NPPES taxonomy, so nothing is hidden.
**Result:** 0 unmapped rows. NPPES_PRIMARY 5,927 · CODE_DEFAULT 2,310 · CODE_PROFILE 1,828 · NPPES_SECONDARY 468 ·
FACILITY_MAP 410. `code_in_specialty_list` (HiLabs lists this code for the row's specialty) is true for 71.0% of rows
(was 55.5%). CODE_DEFAULT describes the code, not the provider, so C7 never uses it to form a peer group.

## C7 — Cost effectiveness (bonus)
Files: **`data/stage7/C7_cost_effectiveness.xlsx`** (Summary, Providers, Best value by specialty, Code comparisons, Read
Me), `provider_cost_effectiveness.csv`, `code_rate_index.parquet`. Input: the final C6 Rate File (PPO network only, so
HMO-signal providers are never scored), without rates flagged by C4 `rate_check`.

**"Similar" providers = same billing code + same rate component (professional vs facility fee) + same HiLabs specialty + same county** (county from the Census-geocoded best address).
If fewer than 5 providers share that cell, widen to statewide (`peer_scope` says which). Providers with **no HiLabs
specialty** (C6 `CODE_DEFAULT`, mostly hospitals) are compared with each other: specialty group "ALL", same code and same
component, weight = the code's highest HiLabs weight. This scores 309 more providers (2,446 → 2,755).
*Why:* price levels differ by service, by specialty mix and by local market. Comparing a Los Angeles dermatologist's 99213 with a rural
family physician's would measure geography, not efficiency.

| Metric | Definition | Why |
|---|---|---|
| Rate index (per code) | provider rate ÷ peer median | 1.00 = typical, 0.80 = 20% cheaper; the median resists outliers |
| **Weighted cost index** | Σ(w × rate index) ÷ Σw, with w = HiLabs weight of that code for the provider's specialty | codes that dominate a specialty's volume count most, so a cheap rare code can't hide an expensive common one |
| % codes below peer median | share of the provider's codes under the median | consistency: is the provider cheap across the board or on one code? |
| n codes compared | codes with a valid comparison | reliability → `label_confidence` HIGH (≥3) / MEDIUM (2) / LOW (1) |
| median % of Medicare | provider's typical professional rate ÷ Medicare 2026 for its own CA locality | an absolute, payer-independent contract level |

**Codes with too few same-code peers (C7 v2, 2026-10-18).** Most providers are priced on only 1–2 HiLabs codes, so a
same-code comparison was impossible for 1,809 of them. Health Net's professional schedule is **Medicare-pegged** (18% of
standard rates are exactly 100% of the provider's own-locality Medicare; common contract levels are 80%, 90%, 100%, 150%,
170% and 360%). A provider's % of Medicare varies about half as much across its own codes (median log-sd 0.27) as across
providers (0.55), so one code already reveals most of its contract level. For those codes the index becomes
the provider's % of Medicare ÷ the median % of Medicare of its specialty peers (county, else statewide), pooled over codes
(`index_basis` = pct_of_medicare). Rates flagged by C4 `rate_check` (robust outliers, >10× Medicare) are excluded from
every comparison.

**Labels:**
- COST_EFFECTIVE: index ≤ 0.90 **and** ≥ 60% of codes below the median
- HIGH_COST: index ≥ 1.10 **and** ≤ 40% of codes below the median
- INSUFFICIENT_DATA: no code can be compared (label_confidence tells how many codes stand behind a label)
- otherwise AVERAGE

**Result (final, 2026-10-18):** 2,755 providers labelled (409 before the Medicare-normalised index):
COST_EFFECTIVE 532 · AVERAGE 1,587 · HIGH_COST 636; confidence HIGH 580, MEDIUM 544, LOW 1,631.
**External validation:** the labels agree with an independent yardstick. Median professional rate as % of Medicare is
80% for COST_EFFECTIVE, 100% for AVERAGE and 131.7% for HIGH_COST. (Before the change HIGH_COST sat *below* AVERAGE,
142% vs 151%, a sign the 3-code-only labels were noisy.)

### C7 v3: quality, so "cost-effective" means a low price at acceptable quality
A provider that is cheap but poor is not cost-effective. PTD has no quality data, so two CMS sources are joined exactly:

| Who | Source | Join | Covered |
|---|---|---|---|
| Clinicians | CMS Quality Payment Program, **PY 2024 Clinician Public Reporting: Overall MIPS Performance**, Provider Data Catalog dataset `a174-a962` (https://data.cms.gov/provider-data/dataset/a174-a962): quality category score 0–100, else final MIPS score | NPI | 62 providers |
| Hospitals | CMS **Hospital General Information** `xubh-q36u` (https://data.cms.gov/provider-data/dataset/xubh-q36u): hospital overall rating 1–5 stars | CCN → NPI via the CDPH Licensed and Certified Healthcare Facility Listing | 132 providers |

`quality_good` = MIPS ≥ 75 (CMS PY2024 performance threshold) or ≥ 3 stars (national midpoint); both in `config.yaml → c7`.
`value_label` = price at/below peers (weighted index ≤ 1) × quality good: **HIGH_VALUE 70 · HIGH_PRICE_HIGH_QUALITY 65 ·
LOW_PRICE_LOW_QUALITY 33 · LOW_VALUE 26**; **PRICE_ONLY 2,561** where CMS has no quality rating (most organizations, and
clinicians who report only as part of a group). `label_with_quality` turns COST_EFFECTIVE with poor quality into
CHEAP_BUT_LOW_QUALITY.
**Finding:** hospitals priced above their peers have better CMS star ratings (mean 3.40 vs 2.92), a real price/quality
trade-off that a price-only label would hide.
The sheet **Best value by specialty** lists, per specialty, up to five COST_EFFECTIVE providers with HIGH confidence.

**Limits:** price is the negotiated rate, not total spend (PTD has no utilization). Quality covers 194 of 2,755 providers
(CMS rates only some providers); the rest are labelled on price alone and say so (`quality_basis` = NONE).
