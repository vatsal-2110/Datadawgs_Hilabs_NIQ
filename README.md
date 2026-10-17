# Centene PPO Network — Provider Rate Pipeline (HiLabs hackathon)

Finds Centene's **PPO network** (commercial group, California) in its Transparency-in-Coverage files, **grades** the
files, maps every provider to **practicing addresses** with evidence tiers, and produces a provider rate file at
**NPI × TIN × address × billing code** level.

| Deliverable | Where |
|---|---|
| 1. Rate file (Excel) | `data/stage6/provider_rate_file.xlsx` (sheets: Rate File, Rate Variants, Read Me) |
| 2. Logic file | `docs/logic_ppo_network.md` (A: PPO network) + `docs/logic_address_mapping.md` (B: TIN → NPI → address, specialty) + `docs/logic_rate_file_c6_c7.md` (C: rate file, cost effectiveness) |
| 3. File quality metrics | `docs/file_quality_metrics.md` (+ scores in `data/stage3/file_scores.csv`) |
| 4. Summary file | `docs/summary.md` (+ `data/summary.json`), generated from the run |
| 5. Video script | `docs/video_script.md` |
| 6. Code | this repo; re-runs with one command, no hardcoded paths (all paths/URLs in `config.yaml`) |
| 7. Bonus | UI: `app/streamlit_app.py`; cost effectiveness (C7): `data/stage7/provider_cost_effectiveness.csv` |

Full decision log (every rule with alternatives and limitations): `docs/logic_memberA.md`. Column definitions: `SCHEMAS.md`.

## Setup
Python 3.11+.
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
| Stage | Module | What it does |
|---|---|---|
| C1 | `src.stage1_ingest_index` | streams 11 index files → plans, unique rate files (URL dedup), scope flags with reasons |
| C2 step 0 | `src.stage2_profile_rate_file` | facts about the in-scope rate file (one price list per provider, labels, coverage) |
| hand-off | `src.stage4_tin_npi_pairs` | every (NPI, TIN) in the PPO file, with business-name network hints |
| C2 | `src.stage2_plan_labels` | labels every CA plan: product type + provider network, with evidence and confidence |
| C4 | `src.stage4_extract_rates` | streams the rate file, keeps HiLabs codes, dollar rates, median per provider × service |
| C3 | `src.stage3_grade_files` | grades the used file + same-payer comparison files, **in parallel**, 7 weighted metrics |
| C5 | `src.stage5_fetch_sources` | NPPES API + CMS Doctors & Clinicians, **parallel and cached** |
| C5 | `src.stage5_address_map` | candidate addresses → shared standardizer (`src/common/address.py`) + Census geocoder → evidence → VERIFIED / PROBABLE / UNVERIFIED |
| C5 | `src.stage5_audit` | evidence behind the tier rules (recency test, source triangulation) and the NPPES-only baseline vs the pipeline |
| C5 | `src.stage5_specialty` | specialty per NPI (HiLabs crosswalk → NUCC-verified facility map → code profile) |
| C6 | `src.stage6_build_rate_file` | final Excel at NPI × TIN × address × code |
| C7 | `src.stage7_cost_effectiveness` | peer-relative cost index (same code + specialty + county) |
| — | `src.summary` | summary file from the outputs |

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
| CMS facility registries (hospital, ASC, DME, home health) | official addresses of organizations, which the Doctors & Clinicians file doesn't cover |
| CMS Doctors & Clinicians (monthly) | Medicare-enrolled practice addresses and group names: a fresher, independent address source |
| US Census geocoder | address standardization and existence check; county for peer groups |
| NUCC taxonomy CSV | taxonomy code descriptions; verifies the facility-specialty map |
| Health Net public plan pages / EOA flyer / EPO handbook | meaning of EOA and EPO (plan-type evidence) |
