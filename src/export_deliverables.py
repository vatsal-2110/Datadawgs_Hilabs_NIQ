"""Copy the PS deliverables into one tracked folder (config paths.deliverables) at the end of every run.

data/ holds every intermediate output and is not committed; the deliverables a reviewer needs are copied here so the
repository always carries the latest ones:
  1 rate file (Excel)              provider_rate_file.xlsx  (+ .csv)
  4 summary file                   summary.md, summary.json
  C1 / C2 / C7 workbooks           C1_index_plans_and_files.xlsx, C2_ppo_network.xlsx, C7_cost_effectiveness.xlsx
  C3 scores                        file_quality_scores_*.csv (scorecard per file type)
The logic files (deliverable 2) and the file quality metrics (deliverable 3) live in docs/.

Usage: python -m src.export_deliverables
"""
from __future__ import annotations

import shutil

from src.common.normalize import ROOT, load_config


def main():
    cfg = load_config()
    P = cfg["paths"]
    out = P["deliverables"]
    out.mkdir(parents=True, exist_ok=True)
    files = {
        P["stage6"] / "provider_rate_file.xlsx": "provider_rate_file.xlsx",
        P["stage6"] / "provider_rate_file.csv": "provider_rate_file.csv",
        P["stage1"] / "C1_index_plans_and_files.xlsx": "C1_index_plans_and_files.xlsx",
        P["stage2"] / "C2_ppo_network.xlsx": "C2_ppo_network.xlsx",
        P["stage7"] / "C7_cost_effectiveness.xlsx": "C7_cost_effectiveness.xlsx",
        P["stage3"] / "scorecard_in_network.csv": "file_quality_scores_in_network.csv",
        P["stage3"] / "scorecard_allowed.csv": "file_quality_scores_allowed_amounts.csv",
        P["stage3"] / "scorecard_index.csv": "file_quality_scores_index.csv",
        ROOT / "docs" / "summary.md": "summary.md",
        ROOT / "data" / "summary.json": "summary.json",
    }
    for src, name in files.items():
        if not src.exists():
            print(f"WARNING: {src} missing (run the pipeline first)")
            continue
        try:
            shutil.copy2(src, out / name)
        except PermissionError:
            print(f"WARNING: {out / name} is open in another program; close it and re-run")
    print(f"deliverables in {out}: {sorted(p.name for p in out.iterdir())}")


if __name__ == "__main__":
    main()
