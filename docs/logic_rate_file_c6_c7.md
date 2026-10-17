# Logic file — Part C: the provider rate file (C6) and cost effectiveness (C7)

## C6 — One file at NPI × TIN × address × billing code
File: `data/stage6/provider_rate_file.xlsx`. Sheets: **Rate File** (deliverable), **Rate Variants** (every extracted rate), **Read Me**.
Required columns come first: NPI, TIN, Address, Billing Code, Specialty, Rate. Then supporting columns: tier, place of service,
modifier, variant min/max, specialty source, network hint, evidence.

### D6.1 Which rate goes in the single Rate column
After C4, 619 of 6,642 NPI × TIN × code combinations still have more than one row. The **only** differences are the modifier
(e.g. 95 telehealth, 26 professional component, TC technical component) and the place of service (11 office, 22 outpatient
hospital, …). These are legitimately different prices, not noise.
- **Rule:** prefer the row with **no modifier** (the standard, global service), then the one at **office (POS 11)**, else the most
  common place of service; if several remain, take their median.
- **Why:** this is the like-for-like price contracting teams compare across providers. Mixing a telehealth or technical-component price
  into one number would mislead.
- **Result:** the chosen place of service is office 11 for 3,501 combinations (02 telehealth 436, 22 outpatient 399, …). There is no modifier
  for 4,063. 1,112 use modifier 95, because for those codes (e.g. psychotherapy) the provider is priced **only** with telehealth.
- **Nothing hidden:** `n_rate_variants`, `variant_rate_min`/`max` and the full **Rate Variants** sheet keep every price.

### D6.2 Which addresses
Every VERIFIED and PROBABLE address of the (NPI, TIN). If there is none, the single best UNVERIFIED address, or NONE.
So no rate is dropped for lack of an address, and the tier tells the user how far to trust it.
**Result:** 11,724 rows: VERIFIED 5,424 · PROBABLE 3,968 · UNVERIFIED 2,319 · NONE 13. 2,824 NPIs, 2,231 TINs, 3,455 addresses, 134 codes.

### D6.3 Specialty on each row
The provider's specialty (rules in Part B §3). `code_in_specialty_list` flags whether HiLabs lists the code for that specialty
(true for 58.4% of rows). The rest are legitimate but less typical (e.g. an urgent-care centre billing an office visit), or the
provider is "Unmapped".

## C7 — Cost effectiveness (bonus)
File: `data/stage7/provider_cost_effectiveness.csv` (+ `code_rate_index.parquet`).

**"Similar" providers = same billing code + same HiLabs specialty + same county** (county from the Census-geocoded best address).
If fewer than 5 providers share that cell, widen to statewide (`peer_scope` says which).
*Why:* price levels differ by service, by specialty mix and by local market. Comparing a Los Angeles dermatologist's 99213 with a rural
family physician's would measure geography, not efficiency.

| Metric | Definition | Why |
|---|---|---|
| Rate index (per code) | provider rate ÷ peer median | 1.00 = typical, 0.80 = 20% cheaper; the median resists outliers |
| **Weighted cost index** | Σ(w × rate index) ÷ Σw, with w = HiLabs weight of that code for the provider's specialty | codes that dominate a specialty's volume count most, so a cheap rare code can't hide an expensive common one |
| % codes below peer median | share of the provider's codes under the median | consistency: is the provider cheap across the board or on one code? |
| n codes compared | codes with a valid peer group | reliability |

**Labels:**
- COST_EFFECTIVE: index ≤ 0.90 **and** ≥ 60% of codes below the median
- HIGH_COST: index ≥ 1.10 **and** ≤ 40% of codes below the median
- INSUFFICIENT_DATA: fewer than 3 codes
- otherwise AVERAGE

**Result:** 2,305 providers. COST_EFFECTIVE 32 · AVERAGE 352 · HIGH_COST 112 · INSUFFICIENT_DATA 1,809. Most providers are priced
on only 1–2 HiLabs codes in this file (median 3 among those scored), the same coverage gap C3 reports.

**Limits:** price only. PTD has no quality, outcome or utilization data, so "cost-effective" here means "negotiated prices below similar
providers". A full measure would add utilization (e.g. CMS claims) and quality (e.g. Care Compare ratings).
