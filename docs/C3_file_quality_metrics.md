# File quality metrics (C3)

**Goal.** Tell a contracting team how far it can trust each price-transparency (PTD) file before using it. The PS asks
to "score every file you use", so every file type the pipeline reads is graded, each on **its own job**:

| File type | Files we use | Its job | Comparison peer (same payer, same state) |
|---|---|---|---|
| In-network rate file | Health Net CA in-network (the PPO network's only rate file, C2) | prices agreed with providers | Ambetter CA in-network |
| Allowed-amounts file | Health Net CA allowed-amounts (used in C4 as network evidence and benchmark) | what was paid to out-of-network providers | Ambetter CA allowed-amounts |
| Index files | all 11 Centene index files (C1) | the table of contents: plans → price files | each other |

Peers are California files of the same payer, so differences come from the file, not from the market or the state
(Health Net Oregon was used as a peer earlier and was dropped for that reason).

**How.** `python -m src.stage3_scorecard` (run by `src.run_all` as stage `c3score`). Files are streamed with `ijson`
and graded in parallel processes. Every threshold is in `config.yaml → grading.scorecard`; field rules follow the
official CMS Transparency-in-Coverage schemas. Output: `data/stage3/scorecard_{in_network,allowed,index}.csv`,
`data/stage3/scorecard_report.txt`. Every metric is 0–100 (higher = better); overall = weighted mean, then the Trust Gate.

---

## 1. In-network rate files (prices agreed with providers). Unit: price record

| Metric | Simple question | How it is calculated | Why it matters | Weight |
|---|---|---|---|---|
| **Usable price share** (Contract-Ready Rate Yield) | Out of all the prices we need, how many can we actually use? | % of HiLabs-code services (provider group × service) that are usable: **one** distinct price, plausible (inside the Medicare band when Medicare prices the code, and not a robust peer outlier), and the provider group lists ≥ 1 valid NPI | The single number a contracting team cares about: how much of the file can go straight into a rate benchmark | **30** |
| **Makes sense vs Medicare** (Medicare Reality Check) | Is the price in a normal range compared with what Medicare pays for the same service? | % of benchmarkable professional HiLabs prices between 30% and 1,000% of the 2026 Medicare Physician Fee Schedule (CA median geographic factors, same rule as C4 `rate_check`); coverage reported | An outside, payer-independent yardstick: catches typos, unit errors and placeholder prices that internal checks can't see | **20** |
| **Complete price list per provider** (Specialty Fee-Schedule Depth) | Does each provider have prices for the main services of its own specialty? | per NPI with a HiLabs specialty: share of that specialty's top-10 HiLabs codes (by weight) it is priced on, averaged over NPIs | A file can price many codes in total yet give each provider only one or two, which makes provider-level comparison impossible | **20** |
| **One clear price** (Single-Price Certainty) | Does each provider have one price per service, or several conflicting ones? | % of (provider group, service) combinations, all codes, with exactly one distinct price | Several prices for one service force an arbitrary choice (C4) and signal uncertainty | **15** |
| **Data makes sense together** (Field Coherence) | Do the code, the type of bill and the place of service match each other? | % of prices whose code type, billing class, place of service, modifier, arrangement and rate type follow the CMS schema and agree (e.g. revenue codes are institutional; 26/TC modifiers are professional) | Contradictory fields mean the price can't be read reliably | **5** |
| **Fake filling** (Filler-Free Score) | How many fields hold dummy values? Fewer = better | 100 − % of checked fields with filler: empty `network_name` (required by the schema), NPI 0 or failing the check digit, TIN not 9 digits or one repeated digit | Filler looks like data but carries none; it breaks joins and network separation. `9999-12-31` expiry is **not** counted: the CMS schema prescribes it for open-ended contracts | **5** |
| **Ghost providers** (Live Provider Rate) | How many listed providers are missing from NPPES or stale? Fewer = better | % of NPIs found in NPPES and updated within 5 years (all NPIs up to 5,000, else a fixed sample of 200) | Rates for deactivated or long-silent providers are contracts nobody can use | **5** |

## 2. Allowed-amounts files (what was paid to out-of-network providers). Unit: payment record

Codes with fewer than 20 claims may legally be omitted, so there is no coverage metric.

| Metric | Simple question | How it is calculated | Why it matters | Weight |
|---|---|---|---|---|
| **Usable payment share** (Usable Payment Yield) | Out of all payment records, how many can we actually use? | % of records that are schema-complete, coherent, allowed ≤ billed, inside the Medicare band when benchmarkable, with ≥ 1 valid NPI | The share that can serve as an out-of-network benchmark | **30** |
| **Paid vs billed makes sense** (Paid-to-Billed Realism) | Is the amount paid a sensible part of what the provider billed? | % of records with allowed ÷ billed between 0.05 and 1.0 | A payment above the bill, or a token share, means the record is wrong or misattributed | **25** |
| **Makes sense vs Medicare** | Is the payment in a normal range compared with Medicare? | % of benchmarkable professional payments between 30% and 1,000% of Medicare | Same outside yardstick as for in-network | **15** |
| **Matches network prices** (In-Network Anchoring) | For the same service, is the out-of-network payment close to the in-network price? | % of records within 0.5×–5× the same payer's in-network median for the same code and billing class | Out-of-network payments far from the payer's own contracts suggest whole-claim totals or errors | **10** |
| **Data makes sense together** (Field Coherence) | Are codes filed under the right type (e.g. revenue codes as institutional)? | code type and billing class allowed, revenue codes institutional, professional rows carry a place of service, TIN type ein/npi | Wrong types make records unreadable | **10** |
| **Required fields filled** (Schema Completeness) | Are all compulsory fields present? | % of records with every field the CMS allowed-amounts schema requires | Missing required fields = unusable record | **5** |
| **Fake filling** (Filler-Free Score) | Fields filled with dummy values? Fewer = better | 100 − % filler: NPI 0 / bad check digit, placeholder TIN, token billed charge (≤ $1) | As for in-network | **5** |

## 3. Index files (the table of contents). Unit: plan row

| Metric | Simple question | How it is calculated | Why it matters | Weight |
|---|---|---|---|---|
| **Prices can be reached** (Price Reachability) | For each plan, does the link to its price file actually work? | % of plan rows whose in-network file link(s) answer HTTP 200 (HEAD request, cached) | An index that points to dead files makes its plans' prices unavailable | **35** |
| **Correct plan IDs** (Plan ID Integrity) | Are plan IDs real and labelled correctly? | % of plan rows whose `plan_id` follows the CMS rule for its `plan_id_type` (EIN 9 digits; HIOS 10 or 5 characters; placeholders such as 999999999 never valid) | Plan IDs are the join key to employers, regulators and products (C1, C2) | **25** |
| **Clear plan-to-file links** (Plan-to-File Clarity) | Does each plan point to one clear set of price files? | % of distinct plans (brand + normalized name + ID + market) mapped to exactly one set of in-network files | A plan listed with different file sets is ambiguous about which prices apply | **15** |
| **Fake filling** (Filler-Free Score) | Dummy IDs, repeated rows, or identical copied files? | 100 − % rows with placeholder IDs, exact duplicates, or from a byte-identical index file | Copies and placeholders inflate the plan count | **10** |
| **Required fields filled** (Schema Completeness) | Are all compulsory fields present? | every field the CMS table-of-contents schema requires (plan name, ID, ID type, market type, issuer; sponsor for EIN plans) | Missing fields block scope decisions (C1) | **10** |
| **Up to date** (Freshness) | Updated in the last month? | 100 if ≤ 31 days old, then linear to 0 at 365 days | The rule requires monthly updates | **5** |

## Trust Gate (all file types)
A file older than 90 days, or a rate / allowed-amounts file with fewer than 95% valid NPIs, is capped at 50 overall,
however well it scores elsewhere: a stale or unidentifiable file must never look trustworthy.

**Why these weights.** In every file type the biggest weight goes to "can we use it": usable price share, usable
payment share, prices can be reached. External reality checks come next (Medicare, paid vs billed, plan IDs), then
completeness per provider and clarity. Hygiene metrics (coherence, filler, freshness) carry small weights: they rarely
separate files on their own, and the Trust Gate already handles the extreme cases.

---

## Results (this run; regenerated in `docs/summary.md` on every run)

**In-network**

| File | Role | **Overall** | Usable price share | vs Medicare | Price list per provider | One clear price | Coherence | Fake filling | Ghost providers |
|---|---|---|---|---|---|---|---|---|---|
| Health Net CA | **used** | **78.0** | 98.3 | 97.8 | **11.3** | 99.7 | 100 | 78.3 | 55.9 |
| Ambetter CA | comparison | 88.0 | 95.8 | 98.7 | 56.6 | 99.97 | 100 | 99.99 | 63.0 |

**Allowed amounts**

| File | Role | **Overall** | Usable payment share | Paid vs billed | vs Medicare | Matches network prices | Coherence | Required fields | Fake filling |
|---|---|---|---|---|---|---|---|---|---|
| Health Net CA | **used** | **40.2** | 18.8 | 34.2 | 34.9 | 32.1 | 76.4 | 100 | 97.8 |
| Ambetter CA | comparison | 81.3 | 90.5 | 92.2 | 56.1 | 27.6 | 100 | 100 | 99.8 |

**Index files:** Health Net **97.7** (157 invalid plan IDs: HIOS IDs labelled "ein", and 999999999 placeholders) · Ambetter 100 · Fidelis 100 ·
QualChoice and its 7 sub-indexes **55–65**: none of their price-file links answer (Price Reachability 0), and one
sub-index is a byte-identical copy of another.

### What the grades say about the files we use
- **Health Net CA in-network (78.0):** the prices themselves are clean: 98% usable, 98% plausible against Medicare, almost
  always one price per service. The weakness is **depth per provider (11.3 vs 56.6 for Ambetter)**: each provider is
  priced on few of its specialty's main codes, because 99.7% of the file is facility pricing and ~80% of its NPIs are
  organizations. Every `network_name` is empty (the main filler), which is why networks can't be separated inside the
  file (C2). 44% of its NPIs have not updated NPPES in 5 years.
- **Health Net CA allowed-amounts (40.2):** most payment records are not usable as unit prices: allowed amounts are
  often whole-claim totals (e.g. venipuncture 36415 at $857) and far from billed charges. C4 therefore never mixes them
  into rates and uses them only as network evidence and a benchmark.
- **Index files:** Health Net's index is reliable; QualChoice's links are dead, so its plans' prices can't be reached.

### Cross-check: an earlier 7-metric grader (`src.stage3_grade_files`, stage `c3`)
Kept as an independent second opinion on the in-network file. It scores code coverage by HiLabs weight (25), share of
individual NPIs (15), $0 / outlier rates (15), dollar rate types (10), duplicates and conflicts (10), freshness (10),
NPI check digit and TIN validity (15): Health Net CA **81.0** vs Ambetter CA **94.6**. Both graders rank the files the
same way and point to the same weakness (provider-level detail). The scorecard above is the official C3 because it
grades **every** file type we use, including allowed-amounts and index files.

### Limitations
- Medicare benchmarks only professional codes on the Physician Fee Schedule (3.3% of Health Net's HiLabs prices,
  because most are facility prices); coverage is reported next to the score.
- Peer outliers are statistical extremes, not proven errors.
- Weights are a judgment, made explicit and adjustable (`W_IN`, `W_AA`, `W_IX` in `src/stage3_scorecard.py`); the raw
  metrics are always published next to the score.
