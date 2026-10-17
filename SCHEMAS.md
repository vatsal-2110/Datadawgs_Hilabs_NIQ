# Hand-off file schemas

Shared rules: NPI = 10-digit string, TIN = 9-digit zero-padded string. Paths are relative to the repo root.

## Stage 1 — C1 index parsing (`python -m src.stage1_ingest_index`)

### `data/stage1/c1_plan_file_table.parquet` (+ `.csv`): the C1 deliverable
One row per unique (plan_key, file_key).

| column | type | meaning |
|---|---|---|
| brand | str | healthnet / ambetter / fidelis / qualchoice |
| plan_name | str | raw plan name (first spelling seen) |
| plan_id | str | raw plan ID |
| plan_market_type | str | group / individual |
| file_role | str | in_network / allowed_amount |
| rate_file_url | str | download URL of the rate file |
| file_name | str | last path segment of the URL |
| in_scope | bool | CA commercial group (see logic D1.5) |
| scope_reason | str | `in_scope`, or `;`-joined exclusion reasons |
| ca_evidence | str | `;`-joined CA signals (file_state / name_prefix / hios_state) |
| plan_id_type | str | as declared in the index (ein / hios) |
| plan_id_format | str | derived: EIN / HIOS / HIOS_ISSUER / PLACEHOLDER / OTHER / MISSING |
| issuer | str | issuer_name |
| plan_name_variants | str | all raw spellings collapsed into this plan_key |
| source_index_names | str | index files this row came from |
| n_source_rows | int | raw plan rows collapsed into this row |
| plan_key | str | sha1[:16] of brand, normalized name, ID, market type |
| file_key | str | sha1[:16] of the normalized URL (join key to files.parquet) |

### `data/stage1/plans.parquet`
One row per (index file, reporting_structure, plan). Nothing dropped.
plan_row_id, brand, index_name, structure_id, plan_name, plan_name_norm, plan_id, plan_id_type, plan_id_format, id_type_mismatch, plan_market_type, issuer, sponsor, ca_evidence, file_states, file_segments, in_scope, scope_reason, n_files_in_structure, plan_key, n_rows_same_key, dup_in_structure

### `data/stage1/files.parquet`
One row per unique rate file.
file_key, file_name, file_role, descriptions, brands, index_names, n_index_files, n_plan_rows, n_structures, n_in_scope_plans, in_scope, n_url_variants, norm_url, example_url

### `data/stage1/plan_file_map.parquet`
plan_row_id, structure_id, file_key, file_role

### `data/stage1/index_files.parquet`
index_name, brand, url, local_path, bytes, sha256, parsed_at, n_structures, n_plan_rows, last_updated_on, reporting_entity_name, reporting_entity_type, version, identical_to

## Stage 4 — hand-off to Member B (`python -m src.stage4_tin_npi_pairs`)

### `data/stage4/tin_npi_pairs.parquet` (+ `.csv`)
One row per (npi, tin, file_key) found in the in-scope in-network file(s). No rates.

| column | type | meaning |
|---|---|---|
| npi | str (10 digits) or null | null only for TIN-only contracts (placeholder NPI `0` in the file); see `npi_missing` |
| tin | str (9 digits) | dashes removed, zero-padded |
| tin_type | str | `ein` (all rows in the current file) |
| business_names | str | `tin.business_name` values, ` \| `-joined. **Some are network labels, not real names** (see labels) |
| business_name_labels | str | AMBETTER_HMO / AMBETTER_PPO / DIRECT_NETWORK / PPO_COUNTY / HEALTHNET_PPO / HEALTHNET_DIRECT_FFS_COMMERCIAL / IPA, or empty |
| n_provider_groups | int | how many provider_references contain this pair |
| provider_group_ids | str | those provider_group_id values, comma-joined |
| in_capitation_code | bool | priced under at least one `negotiation_arrangement = capitation` code |
| billing_classes | str | professional / institutional / both |
| n_billing_codes | int | distinct billing codes priced for this pair |
| npi_missing | bool | true = TIN-only row |
| n_npis_on_tin | int | NPIs sharing this TIN in the file |
| n_tins_for_npi | Int64 | TINs this NPI bills under in the file (<NA> for TIN-only rows) |
| file_key | str | join key to `data/stage1/files.parquet` |

**Note for Member B:** when `business_names` is a label (e.g. "HEALTH NET DIRECT FFS COMMERCIAL", "PPO: Los Angeles County"), it is **not** the organization's name. Use NPPES / CMS for the real name and address.

### `data/stage4/rates.parquet` (+ `.csv`) — C4 output (`python -m src.stage4_extract_rates`)
One row per (npi, tin, billing_code_type, billing_code, item_name, billing_class, modifier, service_code, additional_information, file_key).

| column | type | meaning |
|---|---|---|
| npi | str (10) or null | null = TIN-only contract (placeholder NPI 0 in the file) |
| tin | str (9) | |
| billing_code_type / billing_code | str | CPT or HCPCS / the code (only HiLabs codes) |
| item_name | str | `name` of the in_network item (needed: one code can carry different services) |
| billing_class | str | professional (all rows in the current file) |
| modifier | str | billing_code_modifier values, `\|`-joined ('' if none) |
| service_code | str | place-of-service codes, `\|`-joined (e.g. `11` office, `22` outpatient hospital) |
| additional_information | str | sub-code from the file, if any |
| rate | float | **median** of the dollar prices for this key |
| rate_min / rate_max | float | lowest / highest price for this key |
| n_rates / n_distinct_rates | int | prices combined / distinct values among them |
| rate_spread_ratio | float | rate_max / rate_min (1.0 = all identical) |
| negotiated_types | str | e.g. `negotiated`, `fee schedule` |
| expiration_date | str | latest expiration_date seen |
| negotiation_arrangement | str | ffs / capitation |
| network_hint | str | business-name label(s) of the provider group (D2.1), '' if none |
| in_capitation_code | bool | priced under a capitation item |
| tin_type | str | ein |
| hilabs_spec_cds | str | HiLabs SPEC_CD values whose list contains this code, comma-joined |
| file_key | str | join key to `data/stage1/files.parquet` |

## Stage 2 — plan labels (`python -m src.stage2_plan_labels`)
`data/stage2/plan_labels.parquet` (+ `.csv`): one row per in-scope plan row. Key columns: plan_row_id, plan_key, plan_name, plan_id, product_type (PPO/HMO/EPO/POS/EOA_HYBRID/UNKNOWN), provider_network (PPO_NETWORK / PPO_NETWORK_INFERRED / HMO_NETWORK / HMO_NETWORK+PPO_SPECIALISTS / UNCONFIRMED), is_ppo, uses_ppo_network, uses_ppo_network_incl_inferred, confidence, rule, evidence, source.

## Stage 3 — file grades (`python -m src.stage3_grade_files`)
`data/stage3/file_scores.parquet` (+ `.csv`): one row per graded file. `role` (USED (in scope) / comparison), raw metrics
(`code_cov_weight_pct`, `type1_pct`, `zero_or_missing_rate_pct`, `outlier_pct`, `dollar_type_pct`, `duplicate_entry_pct`,
`conflict_pct`, `age_days`, `npi_check_digit_ok_pct`, `tin_9digit_pct`, `network_name_filled_pct`, `real_expiry_pct`, …),
scores `M1_code_coverage` … `M7_id_validity` (0–100) and `overall_score`. Definitions: `docs/file_quality_metrics.md`.

## Stage 5 — addresses and specialty
`data/stage5/nppes_npi.parquet`: npi, found, enumeration_type, name, last_updated, taxonomy_codes, primary_taxonomy.
`data/stage5/nppes_addresses.parquet`, `dac_addresses.parquet`, `dac_group_addresses.parquet`, `registry_facilities.parquet`: raw source rows.
`data/stage5/geocode.parquet`: Census result per distinct address (match, std_address, lat, lon, county_fips5).

`data/stage5/npi_tin_address.parquet` (+ `.csv`): one row per (npi, tin, building).
| column | meaning |
|---|---|
| npi, tin | pair from the rate file (npi null for TIN-only contracts) |
| address | display address (Census-standardized when geocoded, suite in brackets) |
| address_1, address_2, city, state, zip, std_address, addr_id | raw and standardized parts; addr_id = building identity |
| lat, lon, county_fips5, tract | from the Census geocoder |
| tier | VERIFIED / PROBABLE / UNVERIFIED / NONE (rules: `docs/logic_address_mapping.md`) |
| sources | e.g. `DAC\|NPPES_LOC`, `NPPES_LOC\|DAC_GROUP`, `NPPES_LOC\|REGISTRY:CMS_HOSPITAL`, `NPPES_ENDPOINT` |
| n_independent_sources | families agreeing: NPPES, CMS enrolment (DAC), CMS facility registry |
| tin_corroboration, npis_on_tin_at_addr | other NPIs of the same TIN report this address in their own records |
| dac_name_match, registry_match, geocoded, in_state | evidence flags |
| nppes_current | NPPES updated within 24 months of the newest record: reported only, never raises a tier (logic 3.1) |
| is_po_box | PO box: never VERIFIED or PROBABLE |
| points, rank_in_pair, is_best | ranking inside the pair |

`data/stage5/c5_audit.json` / `.txt` (`python -m src.stage5_audit`): A recency test, B source triangulation,
C NPPES-only baseline vs pipeline (feeds `summary.json → address_baseline_vs_pipeline`).

`data/stage5/npi_specialty.parquet` (+ `.csv`): npi, enumeration_type, primary_taxonomy(+desc), spec_cd, specialty,
specialty_source (NPPES_PRIMARY / NPPES_SECONDARY / FACILITY_MAP / CODE_PROFILE / UNMAPPED), specialty_note.

## Stage 6 — final rate file (`python -m src.stage6_build_rate_file`)
`data/stage6/provider_rate_file.xlsx` (sheets Rate File / Rate Variants / Read Me) and `.parquet` / `.csv`.
One row per NPI × TIN × Address × Billing Code. Columns: **NPI, TIN, Address, Billing Code, Specialty, Rate**, then provider_name,
npi_type, item_name, billing_code_type, place_of_service, modifier, n_rate_variants, variant_rate_min, variant_rate_max,
address_tier, city, state, zip, county_fips5, lat, lon, spec_cd, specialty_source, code_in_specialty_list, network_hint,
in_capitation_code, sources, evidence. Rate rule: `docs/logic_rate_file_c6_c7.md` D6.1.

## Stage 7 — cost effectiveness (`python -m src.stage7_cost_effectiveness`)
`data/stage7/provider_cost_effectiveness.parquet` (+ `.csv`): NPI, TIN, specialty, spec_cd, county, n_codes_compared,
weighted_cost_index, pct_codes_below_median, share_county_peers, label (COST_EFFECTIVE / AVERAGE / HIGH_COST / INSUFFICIENT_DATA).
`data/stage7/code_rate_index.parquet`: per provider × code: rate, peer_scope, peer_n, peer_median, rate_index, w.

## Summary
`data/summary.json`, `docs/summary.md`: generated by `python -m src.summary`.
