# Run from the project root with the venv active. On Windows without `make`, run the commands directly.
PY ?= python

.PHONY: install stage1 stage1-sample profile pairs labels rates verify test grade fetch address c5audit scope-evidence plan-design network-evidence scorecard medicare contract-terms c1c2excel specialty ratefile cost summary all ui

install:
	$(PY) -m pip install -r requirements.txt

stage1:            ## C1: download + parse all index files in config.yaml
	$(PY) -m src.stage1_ingest_index

stage1-sample:     ## C1 on the synthetic test index
	$(PY) -m src.stage1_ingest_index --sample

profile:           ## C2 step 0: profile the CA in-network rate file (facts only)
	$(PY) -m src.stage2_profile_rate_file

pairs:             ## hand-off to Member B: tin_npi_pairs.parquet from in-scope in-network files
	$(PY) -m src.stage4_tin_npi_pairs

labels:            ## C2 layer 1: label every in-scope plan (product_type + provider_network)
	$(PY) -m src.stage2_plan_labels

rates:             ## C4: extract rates for HiLabs codes from in-scope in-network files
	$(PY) -m src.stage4_extract_rates

verify:            ## independent checks of Member A outputs (C1, labels, pairs, rates)
	$(PY) -m tests.verify_memberA

test:              ## unit tests (no data needed): address standardization, C5 tier rules
	$(PY) -m pytest tests -q

grade:             ## C3: grade the used file + comparison files (parallel)
	$(PY) -m src.stage3_grade_files

fetch:             ## C5: NPPES + CMS Doctors & Clinicians (parallel, cached)
	$(PY) -m src.stage5_fetch_sources

address:           ## C5: candidate addresses, Census geocoding, evidence tiers
	$(PY) -m src.stage5_address_map

c5audit:           ## C5: evidence behind the tier rules + NPPES-only baseline vs pipeline
	$(PY) -m src.stage5_audit

specialty:         ## specialty per NPI
	$(PY) -m src.stage5_specialty

scope-evidence:    ## C1: third-party proof of the CA commercial group scope
	$(PY) -m src.stage1_scope_evidence

plan-design:       ## C2: plan benefit design matched to Health Net 2026 plan documents
	$(PY) -m src.stage2_plan_design_evidence

contract-terms:    ## C4: recover each provider's contract formula; validated estimated rates
	$(PY) -m src.stage4_contract_terms

scorecard:         ## C3: scorecard for in-network, allowed-amounts and index files
	$(PY) -m src.stage3_scorecard

medicare:          ## C4: every rate vs Medicare 2026 (own CA locality) + robust outlier flags
	$(PY) -m src.stage4_medicare_benchmark

network-evidence:  ## C2: plan labels vs HIOS products, DOL Schedule A, legal entity; provider-level PPO evidence
	$(PY) -m src.stage2_network_evidence
	$(PY) -m src.stage2_provider_network

c1c2excel:         ## C1 and C2 Excel deliverables
	$(PY) -m src.export_c1_c2_excel

ratefile:          ## C6: final Excel rate file
	$(PY) -m src.stage6_build_rate_file

cost:              ## C7: cost effectiveness
	$(PY) -m src.stage7_cost_effectiveness

summary:           ## deliverable 4: summary file
	$(PY) -m src.summary

all:               ## whole pipeline in order
	$(PY) -m src.run_all

ui:                ## bonus UI
	streamlit run app/streamlit_app.py
