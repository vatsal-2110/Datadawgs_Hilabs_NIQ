"""Deliverable 4: summary file, generated from the run's outputs (never typed by hand).

Outputs: data/summary.json and docs/summary.md
Usage  : python -m src.summary
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

from src.common.normalize import ROOT, load_config


def main():
    cfg = load_config()
    P = cfg["paths"]
    idx = pd.read_parquet(P["stage1"] / "index_files.parquet")
    plans = pd.read_parquet(P["stage1"] / "plans.parquet")
    files = pd.read_parquet(P["stage1"] / "files.parquet")
    labels = pd.read_parquet(P["stage2"] / "plan_labels.parquet")
    pairs = pd.read_parquet(P["stage4"] / "tin_npi_pairs.parquet")
    rates = pd.read_parquet(P["stage4"] / "rates.parquet")
    scores = pd.read_parquet(P["stage3"] / "file_scores.parquet")
    addr = pd.read_parquet(P["stage5"] / "npi_tin_address.parquet")
    spec = pd.read_parquet(P["stage5"] / "npi_specialty.parquet")
    rf = pd.read_parquet(P["stage6"] / "provider_rate_file.parquet")
    ce_path = P["stage7"] / "provider_cost_effectiveness.parquet"
    ce = pd.read_parquet(ce_path) if ce_path.exists() else None

    npis = pairs.npi.dropna().unique()
    tins = pairs.tin.unique()
    ver = addr[addr.tier == "VERIFIED"]
    pro = addr[addr.tier.isin(["VERIFIED", "PROBABLE"])]
    rf_npis = rf.NPI.dropna().unique()
    S = {
        "run_date": date.today().isoformat(),
        "index_files_parsed": int(len(idx)),
        "plan_rows": int(len(plans)),
        "unique_rate_files": int(len(files)),
        "duplicate_file_references_removed": int(
            pd.read_parquet(P["stage1"] / "plan_file_map.parquet")[["structure_id", "file_key"]]
            .drop_duplicates().shape[0] - len(files)),
        "ca_commercial_group_plans": int(plans.in_scope.sum()),
        "ppo_plans": int(labels.is_ppo.sum()),
        "ppo_plans_incl_epo_on_inferred_ppo_network": int(labels.uses_ppo_network_incl_inferred.sum()),
        "plans_ruled_out": labels[~labels.is_ppo].product_type.value_counts().to_dict(),
        "ppo_rate_files": files[files.in_scope & (files.file_role == "in_network")].file_name.tolist(),
        # official C3 = scorecard (every file type we use); the 7-metric grader is kept as a cross-check
        "file_quality_scores": {k: pd.read_parquet(P["stage3"] / f"scorecard_{k}.parquet")
                                [[c for c in ("file_name", "index_name", "role", "overall_score")
                                  if c in pd.read_parquet(P["stage3"] / f"scorecard_{k}.parquet").columns]]
                                .to_dict("records")
                                for k in ("in_network", "allowed", "index")
                                if (P["stage3"] / f"scorecard_{k}.parquet").exists()},
        "file_quality_crosscheck_7_metric_grader": scores[["file_name", "role", "overall_score"]].to_dict("records"),
        "rate_file_specialty_sources": rf.specialty_source.value_counts().to_dict(),
        "providers_in_ppo_file": {"npi_tin_pairs": int(len(pairs)), "unique_npis": int(len(npis)),
                                  "unique_tins": int(len(tins))},
        "rates_extracted": {"rate_rows": int(len(rates)), "billing_codes": int(rates.billing_code.nunique()),
                            "unique_npis": int(rates.npi.nunique()), "unique_tins": int(rates.tin.nunique())},
        "rates_all_codes": (lambda a: {"rate_rows": int(len(a)), "unique_npis": int(a.npi.nunique()),
                                       "billing_codes": int(a.billing_code.nunique()),
                                       "by_component": a.component.value_counts().to_dict()})(
            pd.read_parquet(P["stage4"] / "rates_all.parquet", columns=["npi", "billing_code", "component"]))
            if (P["stage4"] / "rates_all.parquet").exists() else None,
        "hilabs_rates_by_component": rates.groupby("component").billing_code.nunique().to_dict()
                                     if "component" in rates else None,
        "out_of_network": (lambda o, b: {"payments": int(len(o)), "codes": int(o.billing_code.nunique()),
                                         "npis": int(o.npi.nunique()), "codes_also_in_network": int(len(b)),
                                         "median_oon_allowed_to_in_network": float(b.oon_to_in_network_ratio.median())})(
            pd.read_parquet(P["stage4"] / "oon_allowed.parquet"), pd.read_parquet(P["stage4"] / "oon_vs_in_network.parquet"))
            if (P["stage4"] / "oon_allowed.parquet").exists() else None,
        "contract_terms": (lambda c, e: {"providers": int(len(c)), "own_formula": c.formula_class.value_counts().to_dict(),
                                          "inherited_from_tin": int(c.formula_source.eq("TIN").sum()),
                                          "estimated_rates": int(len(e)), "estimated_providers": int(e.npi.nunique())})(
            pd.read_parquet(P["stage4"] / "provider_contract_terms.parquet"), pd.read_parquet(P["stage4"] / "estimated_rates.parquet"))
            if (P["stage4"] / "estimated_rates.parquet").exists() else None,
        "trust_band_rows": rf.trust_band.value_counts().to_dict() if "trust_band" in rf else None,
        "final_rate_file": {"rows": int(len(rf)), "unique_npis": int(len(rf_npis)), "unique_tins": int(rf.TIN.nunique()),
                            "unique_addresses": int(rf.Address.nunique()), "billing_codes": int(rf["Billing Code"].nunique()),
                            "rows_by_address_tier": rf.address_tier.value_counts().to_dict()},
        "address_linkage": {
            "pct_npis_with_verified_address": round(100 * ver.npi.dropna().nunique() / len(npis), 1),
            "pct_tins_with_verified_address": round(100 * ver.tin.nunique() / len(tins), 1),
            "pct_npis_with_verified_or_probable": round(100 * pro.npi.dropna().nunique() / len(npis), 1),
            "pct_tins_with_verified_or_probable": round(100 * pro.tin.nunique() / len(tins), 1),
            "best_address_tier_per_pair": addr[addr.is_best].tier.value_counts().to_dict()},
        "address_baseline_vs_pipeline": (json.loads((P["stage5"] / "c5_audit.json").read_text(encoding="utf-8"))
                                         ["C_baseline_vs_pipeline"] if (P["stage5"] / "c5_audit.json").exists() else None),
        "plan_network_use": (pd.crosstab(pd.read_parquet(P["stage2"] / "plan_network_prediction.parquet").final_network_use,
                                         pd.read_parquet(P["stage2"] / "plan_network_prediction.parquet").basis)
                             .to_dict(orient="index")
                             if (P["stage2"] / "plan_network_prediction.parquet").exists() else None),
        "ppo_network_evidence_pairs": (pd.read_parquet(P["stage2"] / "provider_network_evidence.parquet")
                                       .ppo_network_evidence.value_counts().to_dict()
                                       if (P["stage2"] / "provider_network_evidence.parquet").exists() else None),
        "rate_rows_by_ppo_evidence": rf.ppo_network_evidence.value_counts().to_dict()
                                     if "ppo_network_evidence" in rf else None,
        "specialty_sources": spec.specialty_source.value_counts().to_dict(),
        "cost_effectiveness_labels": ce.label.value_counts().to_dict() if ce is not None else None,
    }
    (ROOT / "data" / "summary.json").write_text(json.dumps(S, indent=2), encoding="utf-8")

    al = S["address_linkage"]
    md = [f"# Run summary ({S['run_date']})", "",
          "*Generated by `python -m src.summary` from the pipeline outputs.*", "",
          "| Metric | Value |", "|---|---|",
          f"| Index files parsed | {S['index_files_parsed']} |",
          f"| Plan rows in index files | {S['plan_rows']:,} |",
          f"| Unique rate files (after removing {S['duplicate_file_references_removed']} duplicate references) | {S['unique_rate_files']} |",
          f"| CA commercial group plans | {S['ca_commercial_group_plans']:,} |",
          f"| **PPO plans identified** | **{S['ppo_plans']}** (evidence: `data/stage2/c2_network_evidence.txt`) |",
          f"| Plans ruled out (not PPO) | " + ", ".join(f"{k} {v}" for k, v in S['plans_ruled_out'].items()) + " |",
          f"| **PPO rate files** | **{len(S['ppo_rate_files'])}**: {', '.join(S['ppo_rate_files'])} |",
          f"| Providers in the PPO file | {S['providers_in_ppo_file']['unique_npis']:,} NPIs, {S['providers_in_ppo_file']['unique_tins']:,} TINs ({S['providers_in_ppo_file']['npi_tin_pairs']:,} pairs) |",
          f"| Rates extracted (HiLabs codes) | {S['rates_extracted']['rate_rows']:,} rows, {S['rates_extracted']['billing_codes']} codes, {S['rates_extracted']['unique_npis']:,} NPIs, {S['rates_extracted']['unique_tins']:,} TINs |",
          *([f"| All in-network prices (every code type) | {S['rates_all_codes']['rate_rows']:,} rows, {S['rates_all_codes']['billing_codes']:,} codes, {S['rates_all_codes']['unique_npis']:,} NPIs (every provider in the file) |"] if S.get("rates_all_codes") else []),
          *([f"| HiLabs codes priced by component | {S['hilabs_rates_by_component']} (facility = decoded revenue-code prices) |"] if S.get("hilabs_rates_by_component") else []),
          *([f"| Out-of-network allowed amounts (separate table) | {S['out_of_network']['payments']:,} payments, {S['out_of_network']['codes']} codes; median OON allowed / in-network rate {S['out_of_network']['median_oon_allowed_to_in_network']:.1f}x over {S['out_of_network']['codes_also_in_network']} shared codes |"] if S.get("out_of_network") else []),
          *([f"| Contract formulas recovered (X% of Medicare) | {S['contract_terms']['providers']:,} providers: {S['contract_terms']['own_formula']}, +{S['contract_terms']['inherited_from_tin']} from their TIN; {S['contract_terms']['estimated_rates']:,} validated estimated rates (separate) |"] if S.get("contract_terms") else []),
          f"| Final rate file | {S['final_rate_file']['rows']:,} rows, {S['final_rate_file']['unique_npis']:,} NPIs, {S['final_rate_file']['unique_tins']:,} TINs, {S['final_rate_file']['unique_addresses']:,} addresses |",
          f"| **NPIs linked to a VERIFIED address** | **{al['pct_npis_with_verified_address']}%** (verified or probable: {al['pct_npis_with_verified_or_probable']}%) |",
          f"| **TINs linked to a VERIFIED address** | **{al['pct_tins_with_verified_address']}%** (verified or probable: {al['pct_tins_with_verified_or_probable']}%) |"]
    bv = S["address_baseline_vs_pipeline"]
    if bv:
        md += [f"| NPPES-only baseline: NPPES primary independently confirmed | {bv['pct_baseline_confirmed']}% "
               f"(pipeline: {bv['pct_pipeline_verified']}%; {bv['verified_only_at_non_nppes_primary_address']} NPIs verified only "
               f"at a non-NPPES-primary address; {bv['pairs_best_address_not_nppes_primary']} pairs whose best address is not "
               f"the NPPES primary) |"]
    if S["plan_network_use"]:
        nu = S["plan_network_use"]
        md += ["| **Plan network use (C2, from plan design + Health Net plan documents)** | "
               + "; ".join(f"{k}: {sum(v.values()):,} ({v.get('EVIDENCE', 0) + v.get('PRODUCT_DOCUMENT', 0):,} from "
                           f"evidence, {v.get('NAME_ONLY', 0):,} name only)" for k, v in nu.items())
               + " (`data/stage2/c2_plan_design.txt`) |"]
    if S["ppo_network_evidence_pairs"]:
        md += [f"| PPO-network evidence per provider pair (C2) | "
               + ", ".join(f"{k} {v:,}" for k, v in S["ppo_network_evidence_pairs"].items())
               + " (rules: `docs/logic_ppo_network.md` §4.1) |"]
    md += ["", "## File quality scores (C3 scorecard, `docs/file_quality_metrics.md`)", "",
           "| File type | File | Role | Overall (0-100) |", "|---|---|---|---|"]
    for k, rows in S["file_quality_scores"].items():
        md += [f"| {k} | {r.get('file_name') or r.get('index_name')} | {r.get('role', 'index')} | {r['overall_score']} |"
               for r in rows]
    md += ["", "Cross-check (7-metric grader, in-network only): " + "; ".join(
        f"{r['file_name']} {r['overall_score']}" for r in S["file_quality_crosscheck_7_metric_grader"])]
    md += ["", "## Specialty", "",
           "Rate file rows by specialty source (every row has a HiLabs specialty): "
           + ", ".join(f"{k}: {v:,}" for k, v in S["rate_file_specialty_sources"].items()),
           "", f"Per NPI ({len(spec):,} in the file, before the row-level CODE_DEFAULT): "
           + ", ".join(f"{k}: {v:,}" for k, v in S["specialty_sources"].items())]
    if S["cost_effectiveness_labels"]:
        md += ["", "## Cost effectiveness (C7)", "", ", ".join(f"{k}: {v:,}" for k, v in S["cost_effectiveness_labels"].items())]
    (ROOT / "docs" / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
