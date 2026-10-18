# Logic file — Part C: the provider rate file (C6) and cost effectiveness (C7)

## C6 — One file at NPI × TIN × address × billing code
File: `data/stage6/provider_rate_file.xlsx`. Sheets: **Rate File** (deliverable), **Rate Variants** (every extracted rate), **Read Me**.
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
VERIFIED **15**, PROBABLE 10, UNVERIFIED 4. Bands: HIGH ≥ 80 (5,539 rows), MEDIUM 60–79 (3,489), LOW < 60 (1,259).
The Excel also has an **Estimated Rates** sheet (C4 step 9), kept apart from the real rates.

### D6.2 Which addresses
Every VERIFIED and PROBABLE address of the (NPI, TIN). If there is none, the single best UNVERIFIED address, or NONE.
So no rate is dropped for lack of an address, and the tier tells the user how far to trust it.
**Result (C4 v2, 2026-10-18):** 10,287 rows: VERIFIED 6,940 · PROBABLE 213 · UNVERIFIED 3,121 · NONE 13. 2,995 NPIs, 2,356 TINs,
3,035 addresses, 203 codes (facility prices for 176 hospital/ASC NPIs now included).
Every extracted (NPI, TIN, code) is present. The file has fewer rows than in the first run (11,724) because an NPPES address
no longer counts as PROBABLE just because the NPPES record was updated recently (Part B §3.1): those pairs now carry their
single best address, labelled UNVERIFIED, instead of several weakly supported ones.

### D6.3 Specialty on each row
The provider's specialty (rules in Part B §3). `code_in_specialty_list` flags whether HiLabs lists the code for that specialty
(true for 55.5% of rows). The rest are legitimate but less typical (e.g. an urgent-care centre billing an office visit), or the
provider is "Unmapped".

## C7 — Cost effectiveness (bonus)
File: `data/stage7/provider_cost_effectiveness.csv` (+ `code_rate_index.parquet`).

**"Similar" providers = same billing code + same rate component (professional vs facility fee) + same HiLabs specialty + same county** (county from the Census-geocoded best address).
If fewer than 5 providers share that cell, widen to statewide (`peer_scope` says which).
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

**Result:** 2,426 providers labelled (was 409 before the Medicare-normalised index; the rest were INSUFFICIENT_DATA):
COST_EFFECTIVE 459 · AVERAGE 1,408 · HIGH_COST 559; confidence HIGH 507, MEDIUM 474, LOW 1,445.
**External validation:** the labels agree with an independent yardstick. Median professional rate as % of Medicare is
80% for COST_EFFECTIVE, 100% for AVERAGE and 131.5% for HIGH_COST. (Before the change HIGH_COST sat *below* AVERAGE,
142% vs 151%, a sign the 3-code-only labels were noisy.)

**Limits:** price only. PTD has no quality, outcome or utilization data, so "cost-effective" here means "negotiated prices below similar
providers". A full measure would add utilization (e.g. CMS claims) and quality (e.g. Care Compare ratings).
