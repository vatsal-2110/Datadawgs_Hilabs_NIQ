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

### C1 scope evidence (`python -m src.stage1_scope_evidence`)
`data/stage1/scope_evidence.parquet`: one row per reporting structure: brand, structure_id, n_plans, market,
in_network_files, geo_* (NPPES practice state of the file's providers), employer_* (DOL Form 5500 sponsor state of the
plan EINs), hios_* (issuer company / state), E1_market_group, E2_providers, E3_employers, E4_issuer, verdict.
`data/stage1/scope_evidence_plans.parquet`: one row per plan: hios_issuer(_entity/_state), ein, sponsor_state, sponsor_name.

### Excel deliverables (`python -m src.export_c1_c2_excel`)
`data/stage1/C1_index_plans_and_files.xlsx` and `data/stage2/C2_ppo_network.xlsx` (sheet contents in each Read Me sheet).

## Stage 2 — C2 final decisions
`data/stage2/ppo_network_decisions.csv` (+ parquet) and the same columns in `data/stage2/plan_labels.csv`:
| column | meaning |
|---|---|
| final_network_use | PPO_NETWORK_PRIMARY / PPO_NETWORK_TIER (EOA, POS) / HMO_NETWORK_ONLY / EPO_NETWORK |
| is_ppo_network | true = PPO_NETWORK_PRIMARY |
| decided_by | PLAN_DESIGN (Health Net 2026 plan overview / earlier-year design) · OWN_SBC · HSA_DESIGN · NETWORK_BRAND · PRODUCT_DOCUMENT · PLAN_NAME_ONLY |
| evidence | the deciding evidence in words (design, what its plan document says, result) |
| cross_checks | HIOS product, employer DOL Form 5500 Schedule A, and the plan-name word (agrees / differs): not used to decide |
| sources | URL of the exact Health Net plan document / SBC / regulator filing used |
| name_evidence (plan_labels only) | the original plan-name reasoning |

## Stage 2 — C2 evidence
`data/stage2/plan_label_evidence.parquet` (`python -m src.stage2_network_evidence`): per in-scope plan: product_type,
hios_product, t2_product_pure, sched_a_hmo / sched_a_ppo (employer Form 5500 Schedule A), t3_employer_agrees, t4_entity,
t4_regulator, t4_network, t4_source, evidence_confidence (HIGH / MEDIUM / REVIEW).
`data/stage2/provider_network_evidence.parquet` (`python -m src.stage2_provider_network`): per (npi, tin): g_non_hmo_county,
g_counties, l_ppo_label, c_capitation, h_hmo_label, has_rates, ppo_network_evidence (PPO_CONFIRMED / HMO_SIGNAL / MIXED /
PPO_PRESUMED), ppo_evidence_detail. Carried into the rate file as `ppo_network_evidence`, `ppo_evidence_detail`.

## Stage 4 — hand-off to C5 (`python -m src.stage4_tin_npi_pairs`)

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

**Note for C5:** when `business_names` is a label (e.g. "HEALTH NET DIRECT FFS COMMERCIAL", "PPO: Los Angeles County"), it is **not** the organization's name. Use NPPES / CMS for the real name and address.

### `data/stage4/rates_all.parquet` / `rates.parquet` (+ `.csv`) — C4 output (`python -m src.stage4_extract_rates`)
`rates_all` = every in-network price, every code type (CPT, HCPCS, RC), every provider. `rates` = its HiLabs-code subset
(professional and facility), used by C5 specialty, C6 and C7. One row per (npi, tin, billing_code_type, billing_code,
item_name, billing_class, modifier, service_code, revenue_code, negotiation_arrangement, file_key).

| column | type | meaning |
|---|---|---|
| npi | str (10) or null | null = TIN-only contract (placeholder NPI 0 in the file) |
| tin | str (9) | |
| billing_code_type / billing_code | str | CPT / HCPCS / RC and the code. For a facility price decoded from an RC item, the CPT/HCPCS code named in its `additional_information` |
| item_name | str | `name` of the in_network item; for decoded facility prices "facility price under revenue code N" (the RC item names are in `source_items`) |
| billing_class / component | str | professional / institutional; component = professional / facility |
| revenue_code | str | RC the facility price was filed under ('' for professional prices) |
| source_items | str | in_network items that carried this price (`<type> <code> <name>`) |
| modifier | str | billing_code_modifier values, `\|`-joined ('' if none) |
| service_code | str | place-of-service codes, `\|`-joined (e.g. `11` office, `22` outpatient hospital) |
| rate | float | price in the **primary contract** (provider_reference pricing the most services); median if tied |
| primary_contract | int | that provider_reference id |
| rate_source | str | `single contract` or `primary of N contracts` |
| rate_median_all | float | median over all contracts pricing this key |
| rate_min / rate_max | float | lowest / highest price for this key |
| n_rates / n_contracts / n_distinct_rates | int | prices / contracts / distinct contract-level prices |
| rate_spread_ratio | float | rate_max / rate_min (1.0 = all identical) |
| negotiated_types | str | e.g. `negotiated`, `fee schedule` |
| expiration_date | str | latest expiration_date seen |
| negotiation_arrangement | str | ffs / capitation |
| network_hint | str | business-name label(s) of the provider group (D2.1), '' if none |
| in_capitation_code | bool | priced under a capitation item |
| tin_type | str | ein |
| hilabs_spec_cds | str | HiLabs SPEC_CD values whose list contains this code, comma-joined |
| file_key / file_date | str | join key to `data/stage1/files.parquet` / the file's last_updated_on |
| is_hilabs_code | bool | (rates_all only) code is in the HiLabs list |
| medicare_rate / medicare_source / medicare_locality | float / str / str | (rates only, added by `src.stage4_medicare_benchmark`) Medicare 2026 price: PFS for the provider's CA locality (professional) or OPPS national (facility) |
| pct_of_medicare | float | rate / medicare_rate × 100 |
| peer_robust_z | float | 0.6745 × (log rate − median) / MAD among rows with the same code, component, modifier (groups ≥ 5) |
| rate_check | str | OK or HIGH_VS_PEERS / LOW_VS_PEERS / ABOVE_10X_MEDICARE / BELOW_30PCT_MEDICARE (kept, not dropped) |

| contract_multiplier_pct / fits_contract_formula | float / bool | (added by `src.stage4_contract_terms`) provider's recovered formula, X% of Medicare (EXACT/LIKELY only), and whether this rate fits it (±1%) |

### `data/stage4/provider_contract_terms.parquet` (+ `.csv`)
One row per (npi, tin) with ≥ 1 benchmarked professional rate: n_codes, multiplier_pct, n_codes_on_multiplier,
formula_class (EXACT / LIKELY / SINGLE / NONE), tin_* (the same fit over the whole TIN), formula_source (own / TIN),
used_multiplier_pct, used_formula_class, whole_number_pct, medicare_locality, spec_cd, specialty, estimates_ship.

### `data/stage4/estimated_rates.parquet` (+ `.csv`): NOT real rates
npi, tin, billing_code, spec_cd, specialty, medicare_locality, medicare_rate_nonfacility, multiplier_pct,
estimated_rate (= multiplier × Medicare), formula_class, formula_source, validated_within_5pct (hide-and-check accuracy
of that formula group), confidence (HIGH own EXACT / MEDIUM TIN EXACT), is_estimate = True.

### `data/stage4/oon_allowed.parquet` (+ `.csv`), `oon_vs_in_network.parquet`
Out-of-network allowed amounts from the PPO plans' allowed-amounts file: one row per (billing_code, tin, npi, payment):
billing_code_type, billing_code, item_name, tin, npi, service_code, billing_class, allowed_amount, billed_charge, file_key.
`oon_vs_in_network`: per (billing_code, billing_class), in_network_median, in_network_providers, oon_allowed_median,
oon_billed_median, oon_payments, oon_providers, oon_to_in_network_ratio, is_hilabs_code.

## Stage 2 — plan labels (`python -m src.stage2_plan_labels`)
`data/stage2/plan_labels.parquet` (+ `.csv`): one row per in-scope plan row. Key columns: plan_row_id, plan_key, plan_name, plan_id, product_type (PPO/HMO/EPO/POS/EOA_HYBRID/UNKNOWN), provider_network (PPO_NETWORK / PPO_NETWORK_INFERRED / HMO_NETWORK / HMO_NETWORK+PPO_SPECIALISTS / UNCONFIRMED), is_ppo, uses_ppo_network, uses_ppo_network_incl_inferred, confidence, rule, evidence, source.

## Stage 3 — file grades (`python -m src.stage3_grade_files`)
`data/stage3/file_scores.parquet` (+ `.csv`): one row per graded file. `role` (USED (in scope) / comparison), raw metrics
(`code_cov_weight_pct`, `type1_pct`, `zero_or_missing_rate_pct`, `outlier_pct`, `dollar_type_pct`, `duplicate_entry_pct`,
`conflict_pct`, `age_days`, `npi_check_digit_ok_pct`, `tin_9digit_pct`, `network_name_filled_pct`, `real_expiry_pct`, …),
scores `M1_code_coverage` … `M7_id_validity` (0–100) and `overall_score`. Definitions: `docs/C3_file_quality_metrics.md`.

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
| tier | VERIFIED / PROBABLE / UNVERIFIED / NONE (rules: `docs/C5_logic_TIN_NPI_address_and_specialty.md`) |
| sources | e.g. `DAC\|NPPES_LOC`, `NPPES_LOC\|DAC_GROUP`, `NPPES_LOC\|REGISTRY:CMS_HOSPITAL`, `CDPH\|NPPES_LOC`, `NPPES_ENDPOINT` |
| n_independent_sources | families agreeing: NPPES, CMS enrolment (DAC), CMS facility registry, CDPH state licence |
| expected_accuracy | measured share of this tier's best addresses that match the CDPH state licence, on a mapping built without CDPH (hold-out audit, logic §3.6): VERIFIED 0.975, UNVERIFIED 0.86, PROBABLE blank, NONE 0 |
| address_exists_by | CENSUS_GEOCODER or GOVERNMENT_RECORD (a non-NPPES government source lists the same building; logic D5.10); blank = existence not proven |
| tin_corroboration, npis_on_tin_at_addr | other NPIs of the same TIN report this address in their own records |
| dac_name_match, registry_match, geocoded, in_state | evidence flags |
| nppes_current | NPPES updated within 24 months of the newest record: reported only, never raises a tier (logic 3.1) |
| is_po_box | PO box: never VERIFIED or PROBABLE |
| points, rank_in_pair, is_best | ranking inside the pair |

`data/stage5/c5_cdph_audit.parquet`: every hold-out address refereed by CDPH: npi, tin, addr_id, tier, is_best,
same_building (same street+ZIP5 or within 250 m), same_zip.

`data/stage5/c5_audit.json` / `.txt` (`python -m src.stage5_audit`): A recency test, B source triangulation,
C NPPES-only baseline vs pipeline (feeds `summary.json → address_baseline_vs_pipeline`).

`data/stage5/npi_specialty.parquet` (+ `.csv`): npi, enumeration_type, primary_taxonomy(+desc), spec_cd, specialty,
specialty_source (NPPES_PRIMARY / NPPES_SECONDARY / FACILITY_MAP / CODE_PROFILE / UNMAPPED), specialty_note.

## Stage 6 — final rate file (`python -m src.stage6_build_rate_file`)
`data/stage6/provider_rate_file.xlsx` (sheets Rate File / Rate Variants / Read Me) and `.parquet` / `.csv`.
One row per NPI × TIN × Address × Billing Code. Columns: **NPI, TIN, Address, Billing Code, Specialty, Rate**, then provider_name,
npi_type, item_name, billing_code_type, place_of_service, modifier, n_rate_variants, variant_rate_min, variant_rate_max,
address_tier, city, state, zip, county_fips5, lat, lon, spec_cd, specialty_source, code_in_specialty_list, network_hint,
in_capitation_code, sources, evidence. Rate rule: `docs/C6_C7_logic_rate_file_and_cost_effectiveness.md` D6.1.
Sheets: Summary, Rate File, Rate Variants, Estimated Rates, Read Me. Rate File columns added 2026-10-18: `specialty_source` now includes CODE_DEFAULT (provider has no HiLabs specialty: HiLabs specialty that owns the billing code), `provider_taxonomy` (provider's own NPPES taxonomy), `address_expected_accuracy`, `trust_score` / `trust_band` / `trust_detail`, `pct_of_medicare`, `rate_check`, `contract_multiplier_pct`.

## Stage 7 — cost effectiveness (`python -m src.stage7_cost_effectiveness`)
`data/stage7/provider_cost_effectiveness.parquet` (+ `.csv`): NPI, TIN, specialty, spec_cd, county, n_codes_compared,
weighted_cost_index, pct_codes_below_median, share_county_peers, label (COST_EFFECTIVE / AVERAGE / HIGH_COST / INSUFFICIENT_DATA).
`data/stage7/code_rate_index.parquet`: per provider × code: rate, peer_scope, peer_n, peer_median, rate_index, w.

## Summary
`data/summary.json`, `docs/summary.md`: generated by `python -m src.summary`.
