"""Run the whole pipeline in order (cross-platform alternative to `make all`).

Usage (from project root, venv active):
  python -m src.run_all              # every stage
  python -m src.run_all --from c3    # start from a stage (c1, profile, pairs, labels, rates, c3, fetch, c5, c4bench, c2providers, c5audit, specialty, c6, c7, summary)
"""
from __future__ import annotations

import argparse
import importlib
import sys
import time

STAGES = [
    ("c1", "src.stage1_ingest_index"),          # parse index files
    ("c1evidence", "src.stage1_scope_evidence"),  # third-party proof of CA / group scope (NPPES, DOL, regulators)
    ("profile", "src.stage2_profile_rate_file"),  # facts about the in-scope rate file
    ("pairs", "src.stage4_tin_npi_pairs"),      # provider list (NPI, TIN)
    ("labels", "src.stage2_plan_labels"),       # C2 plan labels
    ("c2design", "src.stage2_plan_design_evidence"),  # network use predicted from plan design + Health Net plan documents
    ("c2evidence", "src.stage2_network_evidence"),  # labels tested vs HIOS products, DOL Schedule A, legal entity
    ("rates", "src.stage4_extract_rates"),      # C4 rates
    ("c3", "src.stage3_grade_files"),           # C3 file grades (parallel)
    ("fetch", "src.stage5_fetch_sources"),      # NPPES + CMS DAC (parallel, cached)
    ("c5", "src.stage5_address_map"),           # addresses + tiers (Census geocoder)
    ("c4bench", "src.stage4_medicare_benchmark"),  # rate vs Medicare (own CA locality) + robust outlier flags (needs C5 county)
    ("c2providers", "src.stage2_provider_network"),  # PPO-network evidence per provider (needs C5 addresses)
    ("c1c2excel", "src.export_c1_c2_excel"),    # final C1 and C2 Excel workbooks
    ("c5audit", "src.stage5_audit"),            # evidence behind the C5 rules + NPPES-only baseline
    ("specialty", "src.stage5_specialty"),      # specialty per NPI
    ("c6", "src.stage6_build_rate_file"),       # final Excel
    ("c7", "src.stage7_cost_effectiveness"),    # bonus: cost effectiveness
    ("summary", "src.summary"),                 # summary file
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="c1", choices=[s for s, _ in STAGES])
    args = ap.parse_args()
    names = [s for s, _ in STAGES]
    for name, mod in STAGES[names.index(args.start):]:
        t = time.time()
        print(f"\n===== {name} ({mod}) =====", flush=True)
        sys.argv = [mod]
        importlib.import_module(mod).main()
        print(f"===== {name} done in {time.time() - t:.0f}s =====", flush=True)


if __name__ == "__main__":
    main()
