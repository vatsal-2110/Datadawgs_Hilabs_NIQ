# CLAUDE.md: Project context (HiLabs hackathon, Member A)

This file hands off a previous chat. Read all of it before doing anything.

## 0. How to work with me (the user)
- I'm Member A of a 3-person team in a 2-day hackathon. Help me finish my part (C1, C2, C4) really well.
- Explain in **simple, easy language**. Define any new term.
- Work **step by step**. After each step, tell me exactly which command to run, from which folder, and what output to send back.
- If you write or change a file, tell me its path. If I must paste code myself, tell me **exactly where to paste it** every time.
- Every design decision must be logged as **Decision → Alternatives → Why → Limitation**, in `docs/logic_memberA.md`. The judges score logic, not tooling.
- Don't invent facts about the data. Look at the real files and record what they show, as evidence.

## 1. The problem statement (HiLabs) in short
US health insurers must publish the prices they pay every doctor and hospital (Price Transparency Data, PTD) as huge JSON files (Machine-Readable Files, MRFs).
**Goal:** build a data pipeline that finds Centene's **PPO network** in its PTD files, grades the quality of those files, and maps each provider to their correct practicing address. It produces **one rate file at NPI × TIN × billing code × address level**.

**Scope:**
- Payer: Centene
- Market: **Commercial group, California**
- Network: **PPO**. We must identify which plans and files make it up, and explain how HMO, EPO and POS were ruled out.
- Providers: Type 1 (individual) and Type 2 (organization) NPIs

**Data provided:**
1. Centene index files (Ambetter, Health Net, QualChoice, Fidelis): https://www.centene.com/price-transparency-files.html
2. NPPES extract: CA NPIs with type, taxonomy, primary and secondary addresses. About 80% of addresses are stale.
3. NUCC taxonomy: taxonomy code → specialty
4. Specialty → Billing Code mapping: HiLabs Excel, now at `data/raw/Rate Benchmarking Individual Weights_Hackathon.xlsx` (config `paths.hilabs_xlsx`). It has 3 sheets: Taxonomy-Spec Xwalk (213 Individual taxonomies → 48 specialties), Specialty Weights (48), Spec-Proc Weights (946 rows, 583 codes). 134/583 codes are priced in the CA file (68.8% by weight), all professional. Details in `docs/logic_memberA.md`.

Other public sources are allowed if we explain what each adds (CMS Doctors & Clinicians file, payer directory APIs, address standardization).

**Challenges:**
- C1: Parse the index files into one table (brand, plan name, plan ID, market type, rate file URL), with duplicate files removed.
- C2: Find the PPO network. Explain the logic and how HMO, EPO and POS were ruled out.
- C3: Grade the files (code coverage, provider coverage, $0 and outlier rates, rate types, duplicates, staleness). Justify each metric.
- C4: Extract the rates for every PPO provider. Where a provider has several rates for one code, state which one we keep and why.
- C5: Map TINs and NPIs to their practicing addresses. Don't rely on NPPES alone. One TIN can have many addresses, and one NPI can have many addresses.
- C6: Build the provider rate file.
- C7 (bonus): Cost-effectiveness vs. similar providers.

Running MCheck/R3 on NPPES addresses is a **baseline only**. The judges want thinking beyond it.

**Deliverables:**
1. Rate file in Excel: NPI, TIN, Address, Billing Code, Specialty, Rate
2. Logic file: PPO identification, plus the TIN→NPI→address linking (sources, rules, conflicts)
3. File quality metrics document
4. Summary file: index files parsed, PPO plans and files found, quality scores, rates extracted, unique NPIs and TINs, % of NPIs with a verified address
5. Video, at most 5 minutes
6. Code repo that re-runs from the README with no hardcoded paths
7. Bonus: exec/analyst UI and C7

**Scoring:** Logic 40 (PPO identification, TIN/NPI/address/specialty linking), Metrics 35 (C3), Structure 25 (logic file, summary, video, sensible order).

## 2. Team split
- **Member A (me): C1, C2, C4.** Hands off `plans.parquet`, `files.parquet`, `ppo_decisions.parquet`, `rates.parquet`, and **`tin_npi_pairs.parquet` as early as possible** (B is blocked without it). Writes the PPO section of the logic file.
- **Member B: C5.** NPPES, CMS DAC and NUCC into DuckDB, address standardization, candidate addresses scored with evidence, tiers (Verified / Probable / Unverified). Hands off `npi_tin_address.parquet`.
- **Member C: C3, C6, C7, UI, docs.** Grading, final Excel, Streamlit UI, README, Makefile, video.

**Shared rules:**
- NPI is always a 10-digit string. TIN is always a 9-digit string (zero-padded).
- One repo, one `config.yaml`, no absolute paths.
- Each stage writes Parquet to `data/stageN/`.

## 3. Facts already found in the real data (use as evidence)
- Centene's page lists these index files:
  - `2026-09-30_healthnet_index.json`
  - `2026-09-28_ambetter_index.json`
  - `2026-09-28_fidelis_index.json`
  - `2026-09-28_qualchoice_index.json`, plus 7 QualChoice sub-indexes
- The page notes: "MHN is now included in Ambetter CA for marketplace plans and in Healthnet for commercial plans."
  - So **Health Net = CA commercial group.** BUT the Health Net index also has an **Oregon** structure (381 plans, `healthnet-or_*` files). CA scope needs CA evidence (file token `-ca_`, name `CA …`, HIOS state CA), not the brand alone.
  - **C1 done (2026-10-17):** 11 index files, 3,275 plan rows, 66 unique rate files, 1,312 in-scope plans, and **only 2 in-scope files** (one CA in-network file of 701.6 MB plus its allowed-amounts file), shared by HMO/PPO/EOA/POS plans. See `data/stage1/c1_report.txt` and `docs/logic_memberA.md`.
  - Fidelis is New York and QualChoice is Arkansas, so both are out of scope.
  - Ambetter is the individual marketplace, so it's out of scope.
- Health Net index header: `reporting_entity_name` "Centene Management Company LLC", type "Third Party Administrator", version "2.0.0", updated 2026-09-30.
- Every Health Net plan seen has `plan_market_type: "group"` and issuer "Healthnet, LLC".
- Plan names carry a network token, inconsistently. Seen:
  - `CA L PPO 30/500/4000/30%`
  - `CA L CUS PPO HSAF ED BRZ Plus Aon NCA`
  - `Gold PPO 500/20` / `Platinum PPO 250/15` (HIOS id 67138CA073)
  - `CA L SC HMO …`, `CA L SAL HMO …` (Salud), `CA L EC HMO …`, `CA L HMO …`
  - `Full Network HMO …`, `WholeCare HMO …`, `SmartCare HMO …`, `CalChoice WholeCare HMO …`, `Salud HMO y Mas …`
  - `CA L EOA 20/0/1500`, `CA L EC EOA …`: **EOA is ambiguous.** Verify what Health Net "EOA" is before labeling it.
  - `CA L CUS POS COUNTY OF SANTA CLARA`: explicit POS, so exclude.
- **Hard cases** to showcase in the logic file and video:
  - `CA L CUS PPO CA IRONWORKERS FWP EPO$15` has both PPO and EPO in the name.
  - `CA L SAL Mex 5/0/1500` has no network token.
  - `CA L CUS CBA EOA Ironworkers FWP`.
- **Data-quality findings:**
  - `plan_id_type` is "ein" even for HIOS-format IDs like `67138CA073`.
  - Placeholder ID `999999999` appears.
  - The same plan name repeats under many EINs (sold to many employers).
  - The same EIN appears under both PPO and HMO plans.

## 3b. STATUS (2026-10-17): whole project built (Member A also took over B's and C's parts)
- Run: `python -m src.run_all` (stages c1 → summary); checks: `python -m tests.verify_memberA` (38 checks); UI: `streamlit run app/streamlit_app.py`.
- Results: 453 PPO plans, 1 PPO rate file, 7,520 rates → final file 11,724 rows; 43.9% NPIs / 40.5% TINs with a VERIFIED address;
  C3 scores: CA 79.2 (used), OR 72.7, Ambetter CA 94.6.
- Docs: README.md, docs/logic_ppo_network.md (A), docs/logic_address_mapping.md (B), docs/logic_rate_file_c6_c7.md (C),
  docs/file_quality_metrics.md, docs/summary.md (generated), docs/video_script.md, docs/logic_memberA.md (full decision log).
- Sections 4–5 below describe the original plan; the code has since grown (stage3, stage5_*, stage6, stage7, summary, run_all, app/).

## 4. Current state of the code (original hand-off; see 3b)
Project root `ptd/`. These are already created; check that they exist:
```
ptd/
  requirements.txt   ijson pandas pyarrow pyyaml requests duckdb openpyxl tqdm matplotlib scikit-learn streamlit
  config.yaml        paths (data/raw, data/stage1, stage2, stage4), the index_files URLs, scope {state: CA, market_type: group}
  .gitignore         data/ .venv/ __pycache__/ *.part
  src/__init__.py, src/common/__init__.py   (empty)
  src/common/normalize.py   load_config(), norm_npi(), norm_tin(), norm_url() (drops the query string), file_name_from_url(), ROOT
  src/stage1_ingest_index.py   C1 (done and tested on a synthetic sample)
  tests/sample_index.json      synthetic sample (may be missing; recreate if needed)
  docs/
```

**What `stage1_ingest_index.py` does:**
- Downloads each index file to `data/raw/index/<brand>.json` and caches it.
- Streams the file with `ijson` over `reporting_structure.item`.
- Writes:
  - `data/stage1/plans.parquet`: plan_row_id, brand, structure_id, plan_name, plan_id, plan_id_type, plan_id_format (EIN/HIOS/PLACEHOLDER), plan_market_type, issuer, sponsor, ca_evidence, in_scope flags
  - `data/stage1/files.parquet`: file_key = sha1 of the normalized URL, norm_url, example_url, file_name, descriptions, brands, n_plan_rows, n_structures, n_in_scope_plans, in_scope
  - `data/stage1/plan_file_map.parquet`
  - `data/stage1/index_files.parquet`: provenance plus sha256
  - `data/stage1/c1_report.txt`
- Nothing is dropped. Scope is flagged with a reason.

Run it from inside `ptd/` with the venv active:
```
python -m src.stage1_ingest_index --only healthnet
python -m src.stage1_ingest_index
```
If the download fails with a 403, use a VPN, or save the JSON from a browser into `data/raw/index/healthnet.json`.

**Pending:**
- Run C1 on the real data and inspect `c1_report.txt`.
- Inspect the in-scope file names and descriptions:
  ```
  python -c "import pandas as pd; f=pd.read_parquet('data/stage1/files.parquet'); f=f[f.in_scope].sort_values('n_plan_rows',ascending=False); print(len(f)); print(f[['file_name','descriptions','n_plan_rows']].head(40).to_string())"
  ```

## 5. Next steps (plan for my part)

### C2: PPO classifier (`src/stage2_network_map.py`)
Output: `data/stage2/ppo_decisions.parquet` plus a decision report.

Four layers of evidence per file:
1. **Plan-name tokens:** regex for PPO / HMO / EPO / POS / EOA / Salud / Full Network / WholeCare / SmartCare / CalChoice on the plan name, file name and description. Label each plan PPO / non-PPO / unknown, with the matched token.
2. **File-level voting:** for each file, the share of its in-scope plans labeled PPO vs. non-PPO. A file is PPO if PPO plans dominate and no strong negative signal exists. Keep the counts as evidence.
3. **Provider-overlap fingerprint:** stream a sample of NPIs/TINs from each candidate rate file (first N provider_references or in_network items). Compute pairwise Jaccard similarity and cluster the files. Unknown files are labeled by similarity to confirmed-PPO vs. confirmed-HMO files.
   - Save a **heatmap PNG**. This is the key slide for the video.
4. **External spot-check (optional):** Health Net public plan docs or provider directory, especially to resolve "EOA".

Final columns: file_key, label (PPO / NOT_PPO / AMBIGUOUS), confidence, evidence text, layer scores.
- Write a documented conflict policy for ambiguous files.
- Log every rule in `docs/logic_memberA.md`.

### C4: Rate extraction (`src/stage4_extract_rates.py`)
- Stream each PPO rate file with `ijson`. Handle `.gz` files, and handle both inline `provider_groups` and top-level `provider_references`.
- Filter early:
  - billing codes in the HiLabs Specialty→Code Excel
  - CA NPIs (from B's NPPES CA list, when available)
  - keep `billing_class`, `negotiated_type`, `service_code` and modifiers
- **Which-rate rule (state it):**
  1. Keep only negotiated dollar amounts (negotiated / fee schedule). Flag percentage and per diem rates separately.
  2. Drop $0 and expired (`expiration_date` < today) rates.
  3. Per (NPI, TIN, code, billing_class, modifier), keep the **median**. Keep min, max and n_rates as audit columns.
- **Outputs:**
  - `data/stage4/rates.parquet`: npi, tin, tin_type, billing_code, billing_code_type, billing_class, modifier, negotiated_type, rate, rate_min, rate_max, n_rates, expiration_date, file_key
  - `data/stage4/tin_npi_pairs.parquet`: npi, tin, tin_type, file_key. **Produce this first for B.**
- Process a documented subset first (one file, top codes), then scale up. Write counts for the summary file.

### Also
- Write `SCHEMAS.md` with the exact columns of the hand-off files.
- Add a Makefile target for each stage.

## 6. What makes our project stand out (keep in mind)
- The PPO overlap heatmap, and explicit handling of the hard cases (Ironworkers PPO+EPO, EOA, no-token plans).
- Honest numbers: how many confirmed, overlap-decided and ambiguous.
- Nothing dropped silently; every exclusion has a reason.
- Data-quality findings reported, which also feed into C3.
- Reproducible: re-runs from the README, config-driven.
