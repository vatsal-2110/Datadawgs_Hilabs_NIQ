# Centene PPO Network — Provider Rate Pipeline (HiLabs hackathon)

Finds Centene's **PPO network** (commercial group, California) in its Transparency-in-Coverage files, **grades** the
files, maps every provider to **practicing addresses** with evidence tiers, and produces a provider rate file at
**NPI × TIN × address × billing code** level.

| PS deliverable | Where (the `deliverables/` folder is refreshed at the end of every run) |
|---|---|
| 1. Rate file (Excel) | **`deliverables/provider_rate_file.xlsx`**: one row per NPI × TIN × Address × Billing Code with NPI, TIN, Address, Billing Code, Specialty, Rate first; sheets Summary, Rate File, Rate Variants, Estimated Rates, Excluded - HMO signals, Read Me (+ `.csv`) |
| 2. Logic file | `docs/logic_ppo_network.md` (a: identifying the PPO network, C1–C4, conflicts §3.8) + `docs/logic_address_mapping.md` (b: TIN → NPI → address, specialty, C5) + `docs/logic_rate_file_c6_c7.md` (rate file C6, cost effectiveness C7). Full decision log: `docs/logic_memberA.md` |
| 3. File quality metrics | `docs/file_quality_metrics.md`; scores in `deliverables/file_quality_scores_*.csv` |
| 4. Summary file | `deliverables/summary.md` (+ `summary.json`), generated from the run |
| 5. Video | recorded separately (max 5 minutes) |
| 6. Code repo | this repo: re-runs from this README with one command; no hardcoded paths (all paths and URLs in `config.yaml`) |
| 7. Bonus | (a) UI with executive and analyst views: `app/streamlit_app.py`; (b) cost effectiveness C7: `deliverables/C7_cost_effectiveness.xlsx` |
| Supporting workbooks | `deliverables/C1_index_plans_and_files.xlsx` (C1 table, unique files, scope evidence), `deliverables/C2_ppo_network.xlsx` (PPO plans with evidence, provider PPO evidence) |

Full decision log (every rule with alternatives and limitations): `docs/logic_memberA.md`. Column definitions: `SCHEMAS.md`.

## Setup
Python 3.10+.
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```
Place the HiLabs-supplied Excel at the path in `config.yaml → paths.hilabs_xlsx`
(default `data/raw/Rate Benchmarking Individual Weights_Hackathon.xlsx`).

## Run everything
```bash
python -m src.run_all            # or: make all
python -m tests.verify_memberA   # independent checks of the outputs (or: make verify)
python -m pytest tests -q        # unit tests, no data needed (or: make test)
streamlit run app/streamlit_app.py
```
Resume from any stage: `python -m src.run_all --from c5`. Internet access is needed on the first run (Centene files,
NPPES API, CMS Doctors & Clinicians API, US Census geocoder, NUCC). Every download and API response is cached under
`data/raw/`, so later runs are offline and fast. If centene.com returns 403, use a VPN or save the index JSON from a
browser into `data/raw/index/<name>.json`.

## Pipeline (each stage writes Parquet to `data/stageN/`)
`python -m src.run_all` runs these in order (`--from <stage>` resumes). Nothing is typed in by hand: file names, years and
every source URL come from `config.yaml` or from the previous stage's output.

| run_all stage | Module | What it does |
|---|---|---|
| `c1` | `src.stage1_ingest_index` | streams the 11 index files → plans, unique rate files (URL dedup), scope flags with reasons |
| `c1evidence` | `src.stage1_scope_evidence` | proves the CA / group scope per structure with third-party data: provider locations (NPPES), employer locations (DOL Form 5500), HIOS issuers |
| `profile` | `src.stage2_profile_rate_file` | facts about the in-scope rate file C1 found (labels, one price list per provider, coverage) |
| `pairs` | `src.stage4_tin_npi_pairs` | every (NPI, TIN) in the PPO file, with business-name network hints |
| `labels` | `src.stage2_plan_labels` | C2: product type + provider network per CA plan, with evidence |
| `c2design` | `src.stage2_plan_design_evidence` | C2: network use predicted from each plan's benefit design vs Health Net's plan documents |
| `c2evidence` | `src.stage2_network_evidence` | C2: labels tested vs HIOS products, DOL Schedule A HMO/PPO declarations, legal entity |
| `rates` | `src.stage4_extract_rates` | C4: every price for every provider; facility (RC) prices decoded; one rate per service (primary contract); out-of-network table |
| `c3` | `src.stage3_grade_files` | C3: grades the used file + comparison files, in parallel, 7 weighted metrics |
| `fetch` | `src.stage5_fetch_sources` | C5: NPPES API, CMS Doctors & Clinicians, CMS facility registries (parallel, cached) |
| `c3score` | `src.stage3_scorecard` | C3: scorecard for in-network, allowed-amounts and index files |
| `c5` | `src.stage5_address_map` | C5: candidate addresses (NPPES, DAC, CMS registries, CDPH state licence) → standardizer + Census geocoder → tiers; hold-out accuracy audit |
| `c4bench` | `src.stage4_medicare_benchmark` | C4: every rate vs Medicare (own CA locality PFS / OPPS), data-currency test, robust outlier flags |
| `c2providers` | `src.stage2_provider_network` | C2: PPO-network evidence per provider (geography, labels, capitation, paid out-of-network, price signature) |
| `c1c2excel` | `src.export_c1_c2_excel` | C1 and C2 Excel deliverables |
| `c5audit` | `src.stage5_audit` | C5: evidence behind the tier rules and the NPPES-only baseline |
| `specialty` | `src.stage5_specialty` | specialty per NPI (HiLabs crosswalk → facility map → code profile) |
| `c4terms` | `src.stage4_contract_terms` | C4: each provider's contract formula (X% of Medicare); validated estimated rates |
| `c6` | `src.stage6_build_rate_file` | C6: final Excel at NPI × TIN × address × code, PPO network only (HMO-signal providers in their own sheet) |
| `c7` | `src.stage7_cost_effectiveness` | C7: peer-relative cost index (same code + component + specialty + county), CMS quality (MIPS, hospital stars) → value label; C7 Excel |
| `summary` | `src.summary` | `data/summary.json` and `docs/summary.md` from the outputs |
| `deliverables` | `src.export_deliverables` | copies the PS deliverables into `deliverables/` |

**Inputs placed by hand:** only the HiLabs Excel (supplied with the problem statement). Everything else is downloaded
by the pipeline from the URLs in `config.yaml` and cached under `data/raw/`.

## Scalability
- **Streaming:** every rate file is read with `ijson` (constant memory); a 700 MB file profiles in ~15 s.
- **Parallel:** C3 grades files in separate processes; API fetches and geocoding run in thread pools.
- **Beyond scope:** brands, states, markets and file lists come from `config.yaml`; stages loop over every in-scope
  file from `files.parquet`, so more states or payers mean config changes, not code changes. For national scale the
  same steps map one-to-one onto Spark/DuckDB jobs (per-file partitioning is already the unit of work).

## Data sources
| Source | What it adds |
|---|---|
| Centene index + rate files | plans, networks, negotiated rates (the subject data) |
| HiLabs Excel | billing codes per specialty, weights, taxonomy → specialty crosswalk |
| NPPES NPI Registry API | NPI type, taxonomies, primary + secondary practice locations, endpoint affiliation addresses, last-updated date |
| CMS facility registries, Provider Data Catalog https://data.cms.gov/provider-data/ (hospital, ASC, DME, home health, dialysis, hospice, inpatient rehab, long-term care hospital, nursing home) | official addresses of organizations, which the Doctors & Clinicians file doesn't cover |
| CMS Doctors & Clinicians (monthly) | Medicare-enrolled practice addresses and group names: a fresher, independent address source |
| California Dept. of Public Health, Licensed and Certified Healthcare Facility Listing (https://data.chhs.ca.gov/dataset/healthcare-facility-locations, data date 2026-10-16) | state licensing address of facilities, matched by NPI (C5 source), and the referee of the C5 tier-accuracy audit |
| CMS Physician Fee Schedule RVU files 2026 (RVU26D) and 2025 (RVU25D); CMS OPPS Addendum B, October 2026 | Medicare price of every rate in its own CA locality (C4 benchmark, contract formulas); 2025 file only for the data-currency test |
| CMS MIPS Overall Performance PY2024 (`a174-a962`) and Hospital General Information star ratings (`xubh-q36u`), https://data.cms.gov/provider-data/ | C7 quality: cost-effective = low price at acceptable quality |
| US Census geocoder | address standardization and existence check; county for peer groups |
| NUCC taxonomy CSV | taxonomy code descriptions; verifies the facility-specialty map |
| Health Net public plan pages / EOA flyer / member form FRM049776EC01 | which company, regulator and network each product type uses (EOA/POS/HMO = Health Net of California on the HMO network; PPO = Health Net Life on the PPO network) |
| US Dept. of Labor Form 5500, 5500-SF, Schedule A (2023–2024) | employer location (C1 scope) and employer-declared HMO / PPO contracts (C2 check); downloaded automatically |
| CMS / state regulator filings (HIOS issuers 67138, 10940, 37903, 70525, 25303; CDI EPO network filing; DMHC license) | which licensed company and state each plan ID belongs to; EPO uses its own network |
