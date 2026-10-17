# File quality metrics (C3)

**Goal.** Tell a contracting team how far it can trust a price-transparency (PTD) in-network file before using its
rates. A file is "good" if it (1) prices the services that matter, (2) names the providers who deliver them,
(3) gives valid, comparable dollar amounts, (4) has no duplicate or contradictory prices, (5) is current, and
(6) uses valid identifiers.

**Which files.** The PS asks to "score every file you use". We use one in-network file (Health Net CA, the PPO
network's only rate file). A lone score is hard to read, so we also grade two files from the **same payer and same
format** as peers: Health Net Oregon (group) and Ambetter California (individual marketplace).

**How.** `python -m src.stage3_grade_files`. Each file is streamed with `ijson` (never loaded whole), and the files are
graded in parallel processes. Results: `data/stage3/file_scores.csv`, `data/stage3/c3_report.txt`.

---

## The seven scored metrics
Each metric is a percentage (0–100, higher = better). Overall = weighted mean.

| # | Metric | Calculation | Why it matters | Weight |
|---|---|---|---|---|
| M1 | **Code coverage** | Σ HiLabs weight of codes with ≥1 positive dollar price ÷ Σ HiLabs weight of all 583 codes (sheet "Spec-Proc Weights") | A file is only useful for benchmarking if it prices the services that matter. Weighting by HiLabs' own weights means missing a high-volume code (99214) costs more than missing a rare one. Count-based coverage is reported too | **25** |
| M2 | **Provider detail** | Share of NPIs that are individuals (NPPES Type 1), from a fixed random sample of 200 NPIs per file (seed 7), looked up in the NPPES API | Rates are compared clinician by clinician. A file listing only organization NPIs hides who delivers care, so rates can't be tied to a specialty or practice address (the problem Member B found: UC Regents appears with a few NPIs, Medicare lists thousands) | **15** |
| M3 | **Rate validity** | 100 − % prices ≤ $0 or missing − % robust outliers. Outlier = modified z-score > 3.5 (Iglewicz & Hoaglin: 0.6745·(x − median)/MAD) within the same service (code, item, billing class, place of service, modifier, sub-code, rate type), groups ≥ 10 prices | $0 and extreme values distort medians and benchmarks. Median/MAD resists the outliers it is looking for; grouping by service avoids flagging legitimately different services | **15** |
| M4 | **Usable rate types** | % prices whose `negotiated_type` is a dollar amount (negotiated, fee schedule, derived) | Percentage-of-charges and per-diem rates can't be compared with fee-for-service dollars without extra data | **10** |
| M5 | **Uniqueness** | 100 − % exact duplicate price entries (same provider group, service, rate) − % conflicting prices (same provider group and service, more than one distinct rate) | Duplicates inflate counts; conflicts force an arbitrary pick, i.e. uncertainty in the extracted rate | **10** |
| M6 | **Freshness** | 100 if `last_updated_on` ≤ 31 days before the run date; then linear down to 0 at 365 days | The Transparency-in-Coverage rule requires files to be updated monthly; older files risk stale contracts | **10** |
| M7 | **ID validity** | mean(% NPI entries passing the NPI check digit (Luhn over "80840" + 9 digits), % TIN entries with 9 digits) | Invalid IDs break the join to NPPES, CMS and addresses (C5) and hint at careless file generation | **15** |

**Why these weights.** Coverage (M1 + M2 = 40) comes first, because a clean file that misses the services or the
clinicians can't be used for contracting. Validity and identifiers (M3 + M7 = 30) come next, because they decide
whether the rates can be trusted and linked. Rate type, uniqueness and freshness (30) matter, but each is narrower.
The weights are in one dictionary (`WEIGHTS` in `src/stage3_grade_files.py`), so a team can re-weight without
touching the metric code.

### Reported but not scored (and why)
| Indicator | Why not scored |
|---|---|
| `network_name` filled % | Optional in practice; we report it because it decides whether networks can be separated *inside* a file (C2) |
| Expiry-date informativeness (% not 9999-12-31) | 0% in all three files, so it can't separate files; "staleness of contracts" can't be measured from dates here, only file freshness (M6) |
| Institutional share, capitation items | Describe the file's content mix, not its quality |

---

## Results (run 2026-10-17)

| File | Role | **Overall** | M1 code cov. | M2 provider detail | M3 validity | M4 types | M5 unique | M6 fresh | M7 IDs |
|---|---|---|---|---|---|---|---|---|---|
| Health Net CA in-network | **used** | **79.2** | 68.8 | 20.1 | 94.3 | 100 | 98.8 | 100 | 99.96 |
| Health Net OR in-network | comparison | 72.7 | 52.2 | 8.5 | 89.1 | 100 | 99.95 | 100 | 100 |
| Ambetter CA in-network | comparison | 94.6 | 86.1 | 98.5 | 93.0 | 99.99 | 93.4 | 100 | 100 |

Raw values behind the scores:

| | Health Net CA | Health Net OR | Ambetter CA |
|---|---|---|---|
| last_updated_on (age) | 2026-10-06 (11 d) | 2026-10-06 (11 d) | 2026-09-28 (19 d) |
| NPIs / TINs | 3,734 / 2,888 | 412 / 229 | 74,031 / 12,072 |
| Items / prices | 1,717 / 1,500,288 | 337 / 18,468 | 4,360 / 247,890 |
| HiLabs codes priced (count) | 134/583 (23.0%) | 52/583 (8.9%) | 382/583 (65.5%) |
| Type 1 share (sample n) | 20.1% (199) | 8.5% (199) | 98.5% (199) |
| $0 rates / outliers | 0% / 5.68% | 0% / 10.87% | 0% / 7.00% |
| Duplicate entries / conflicts | 0.85% / 0.32% | 0% / 0.05% | 6.63% / 0.03% |
| NPI check digit OK / TIN 9 digits | 99.91% / 100% | 100% / 100% | 99.99% / 100% |
| network_name filled | 0% | 0% | 100% (all "Exchange CA") |
| Institutional share | 99.7% | 97.9% | 81.1% |

### What the grades say about the file we use (Health Net CA)
- **Strong:** current (11 days old), all dollar rates, no $0 rates, valid IDs, few duplicates or conflicts.
- **Weak:** **provider detail (20.1)**. About 80% of its NPIs are organizations, so individual clinicians are under-represented.
  **Code coverage is partial (68.8% by weight, 23% by count)**, and every price on a HiLabs code is professional; the file is
  99.7% facility prices on revenue codes outside the HiLabs list.
- **Compared with Ambetter CA** (same payer, same state, marketplace): Ambetter lists 20× more NPIs, almost all individuals,
  and prices 382 HiLabs codes. So the Health Net CA gaps are a property of that file, not of Centene's format.
- **Implication for the rate file:** results are reliable for the providers and codes present, but the PPO network
  is under-represented at clinician level. The summary reports this next to the coverage numbers.

### Limitations
- M2 uses a 200-NPI sample per file (fixed seed, reproducible): about ±5.5 points at 95% confidence near 20%.
- M3's outlier test flags statistical extremes, not errors; some outliers are legitimate (e.g. complex cases).
- Weights are a judgment, made explicit and adjustable; the raw metrics are always published next to the score.
