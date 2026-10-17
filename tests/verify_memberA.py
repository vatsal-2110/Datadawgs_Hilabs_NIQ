"""Independent checks of the whole pipeline (C1-C7, summary, Excel).

Re-derives key results with DIFFERENT code paths than the pipeline (plain json.load / pandas groupby
instead of ijson streaming + dicts) and checks every rule and cross-file consistency.

Usage (from project root, after the pipeline has run):
  python -m tests.verify_memberA
Exit code 0 = all checks passed.
"""
from __future__ import annotations

import json
import re
import sys

import pandas as pd

from src.common.normalize import load_config, norm_npi, norm_tin

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((bool(ok), name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def main():
    cfg = load_config()
    P = cfg["paths"]
    s1, s2, s4 = P["stage1"], P["stage2"], P["stage4"]
    plans = pd.read_parquet(s1 / "plans.parquet")
    files = pd.read_parquet(s1 / "files.parquet")
    table = pd.read_parquet(s1 / "c1_plan_file_table.parquet")
    labels = pd.read_parquet(s2 / "plan_labels.parquet")
    pairs = pd.read_parquet(s4 / "tin_npi_pairs.parquet")
    rates = pd.read_parquet(s4 / "rates.parquet")

    # ---------------- C1: independent recount with json.load
    raw_plans = raw_refs = 0
    for src in cfg["index_files"]:
        d = json.load(open(P["raw_index"] / f"{src['name']}.json", encoding="utf-8"))
        for rs in d["reporting_structure"]:
            raw_plans += len(rs.get("reporting_plans") or [])
            raw_refs += len(rs.get("in_network_files") or []) + (1 if rs.get("allowed_amount_file") else 0)
    check("C1 plan rows = raw index count", len(plans) == raw_plans, f"{len(plans)} vs {raw_plans}")
    check("C1 file_key unique", files.file_key.is_unique, f"{len(files)} files from {raw_refs} references")
    check("C1 every plan reaches the C1 table", set(plans.plan_key) == set(table.plan_key))
    check("C1 table has no duplicate (plan, file)", not table.duplicated(["plan_key", "file_key"]).any())
    check("C1 table required columns present",
          {"brand", "plan_name", "plan_id", "plan_market_type", "rate_file_url"} <= set(table.columns))
    check("C1 every in-scope plan has CA evidence and group market",
          (plans[plans.in_scope].ca_evidence != "").all() and (plans[plans.in_scope].plan_market_type == "group").all())
    check("C1 no in-scope plan from a non-CA state file",
          not plans[plans.in_scope].file_states.str.contains(r"\b(?!CA\b)[A-Z]{2}\b").any())
    ins = files[files.in_scope]
    check("C1 in-scope in-network files = 1", (ins.file_role == "in_network").sum() == 1,
          ", ".join(ins.file_name))

    # ---------------- C2 labels
    ca = plans[plans.in_scope]
    check("Labels: one row per in-scope plan", set(labels.plan_row_id) == set(ca.plan_row_id)
          and labels.plan_row_id.is_unique, f"{len(labels)} rows")
    up = labels.plan_name.str.upper().str.replace(r"CUS(?=PPO|HMO|EPO|POS|EOA)", "CUS ", regex=True)
    has = {k: up.str.contains(rf"\b{k}\b|{k}\$") for k in ["PPO", "HMO", "EPO", "POS", "EOA"]}
    exp_ppo = has["PPO"] & ~has["HMO"] & ~has["EPO"] & ~has["POS"] & ~has["EOA"]
    check("Labels: PPO label == names with PPO and no other network word", (labels.is_ppo == exp_ppo).all(),
          f"PPO={int(labels.is_ppo.sum())}")
    check("Labels: no EOA/HMO/POS/EPO name labelled PPO",
          not labels[labels.is_ppo & (has["EOA"] | has["HMO"] | has["POS"] | has["EPO"])].shape[0])
    check("Labels: no UNKNOWN", (labels.product_type != "UNKNOWN").all())
    check("Labels: EPO network marked inferred",
          (labels[labels.product_type == "EPO"].provider_network == "PPO_NETWORK_INFERRED").all())

    # ---------------- rate file: independent read with json.load (loads 700 MB; slow but different code path)
    rf = cfg["paths"]["raw_rates"] / ins[ins.file_role == "in_network"].file_name.iloc[0]
    print(f"loading {rf.name} with json.load (independent of ijson)...")
    doc = json.load(open(rf, encoding="utf-8"))
    ref_rows = []
    for r in doc["provider_references"]:
        for g in r["provider_groups"]:
            for n in g.get("npi") or []:
                ref_rows.append((r["provider_group_id"], norm_npi(n), norm_tin(g["tin"]["value"])))
    refdf = pd.DataFrame(ref_rows, columns=["ref", "npi", "tin"])
    raw_pairs = refdf[["npi", "tin"]].drop_duplicates()
    check("Pairs: same (npi,tin) set as raw file (incl. TIN-only)",
          len(raw_pairs) == len(pairs) and
          set(map(tuple, raw_pairs.fillna("").values)) == set(map(tuple, pairs[["npi", "tin"]].fillna("").values)),
          f"{len(pairs)} rows")
    check("Pairs: NPI 10-digit / TIN 9-digit", pairs.npi.dropna().str.fullmatch(r"\d{10}").all()
          and pairs.tin.str.fullmatch(r"\d{9}").all())

    # ---------------- C4 rates: full independent recompute with pandas
    hl = pd.read_excel(P["hilabs_xlsx"], sheet_name="Spec-Proc Weights", dtype=str)
    codes = set(hl.PROC_CD)
    long = []
    for it in doc["in_network"]:
        if str(it["billing_code"]) not in codes:
            continue
        for b in it["negotiated_rates"]:
            for p in b["negotiated_prices"]:
                if p.get("negotiated_type") not in ("negotiated", "fee schedule", "derived") or p["negotiated_rate"] <= 0:
                    continue
                for ref in b["provider_references"]:
                    long.append((ref, it["billing_code_type"], str(it["billing_code"]), it.get("name"),
                                 p.get("billing_class"), "|".join(sorted(p.get("billing_code_modifier") or [])),
                                 "|".join(sorted(p.get("service_code") or [])), p.get("additional_information"),
                                 float(p["negotiated_rate"])))
    del doc
    L = pd.DataFrame(long, columns=["ref", "billing_code_type", "billing_code", "item_name", "billing_class",
                                    "modifier", "service_code", "additional_information", "r"])
    L = L.merge(refdf, on="ref")
    keys = ["npi", "tin", "billing_code_type", "billing_code", "item_name", "billing_class", "modifier",
            "service_code", "additional_information"]
    g = L.fillna({"npi": "", "additional_information": ""}).groupby(keys).r.agg(["median", "min", "max", "size"]).reset_index()
    R = rates.fillna({"npi": "", "additional_information": ""})
    m = g.merge(R, on=keys, how="outer", indicator=True)
    check("Rates: same keys as independent recompute", (m._merge == "both").all(),
          f"recompute {len(g)} / parquet {len(R)} / only-one-side {int((m._merge != 'both').sum())}")
    both = m[m._merge == "both"]
    check("Rates: median/min/max/n identical to recompute",
          ((both["median"] - both.rate).abs() < 1e-9).all() and ((both["min"] - both.rate_min).abs() < 1e-9).all()
          and ((both["max"] - both.rate_max).abs() < 1e-9).all() and (both["size"] == both.n_rates).all())
    check("Rates: all codes in HiLabs list", rates.billing_code.isin(codes).all(),
          f"{rates.billing_code.nunique()} codes")
    check("Rates: rate > 0 and min <= rate <= max", (rates.rate > 0).all()
          and ((rates.rate_min <= rates.rate) & (rates.rate <= rates.rate_max)).all())
    check("Rates: every (npi,tin) exists in tin_npi_pairs",
          set(map(tuple, rates[["npi", "tin"]].fillna("").drop_duplicates().values))
          <= set(map(tuple, pairs[["npi", "tin"]].fillna("").values)))
    check("Rates: hilabs_spec_cds filled for every row", rates.hilabs_spec_cds.notna().all())
    hmo = rates.network_hint.str.contains("AMBETTER_HMO|IPA") | rates.in_capitation_code
    check("Rates: D2.7 hinted rows counted", True, f"{int(hmo.sum())} hinted / {len(rates)} rows")

    # ---------------- C3 grades
    sc = pd.read_parquet(P["stage3"] / "file_scores.parquet")
    from src.stage3_grade_files import WEIGHTS
    mcols = list(WEIGHTS)
    check("C3: every metric in [0,100]", ((sc[mcols] >= 0) & (sc[mcols] <= 100)).all().all())
    recomputed = (sc[mcols] * pd.Series(WEIGHTS)).sum(axis=1) / sum(WEIGHTS.values())
    check("C3: overall = weighted mean of metrics", ((recomputed - sc.overall_score).abs() <= 0.06).all())
    check("C3: the used (in-scope) file is graded", (sc.role == "USED (in scope)").sum() == len(ins[ins.file_role == "in_network"]))

    # ---------------- C5 address tiers follow the written rules
    ad = pd.read_parquet(P["stage5"] / "npi_tin_address.parquet")
    a = ad[ad.tier != "NONE"]
    ver_rule = a.geocoded.astype(bool) & ((a.n_independent_sources >= 2) | ((a.n_independent_sources == 1)
                                          & (a.tin_corroboration.astype(bool) | a.dac_name_match.astype(bool))))
    check("C5: every VERIFIED row satisfies the VERIFIED rule", ver_rule[a.tier == "VERIFIED"].all())
    check("C5: every row satisfying the VERIFIED rule is VERIFIED (PO boxes excluded)",
          (a[ver_rule & ~a.is_po_box.astype(bool)].tier == "VERIFIED").all())
    pro_rule = a.geocoded.astype(bool) & (a.sources.str.contains("DAC") | a.registry_match.fillna("").ne("")) & ~ver_rule
    check("C5: every PROBABLE row has a geocoded non-NPPES source (DAC or CMS registry)",
          pro_rule[a.tier == "PROBABLE"].all())
    check("C5: NPPES-only addresses are never above UNVERIFIED (update age is not evidence, audit A)",
          (a[~a.sources.str.contains(r"DAC|REGISTRY", regex=True) & ~a.tin_corroboration.astype(bool)]
           .tier == "UNVERIFIED").all())
    check("C5: no PO box is VERIFIED or PROBABLE", not (a.is_po_box.astype(bool) & a.tier.isin(["VERIFIED", "PROBABLE"])).any())
    check("C5: no address is supported only by DAC_GROUP (confirmation-only source)",
          not a.sources.str.fullmatch(r"DAC_GROUP(\|REGISTRY:.*)?").any())
    check("C5: every (npi,tin) pair has an address row (or NONE)",
          set(map(tuple, pairs[["npi", "tin"]].fillna("").values)) == set(map(tuple, ad[["npi", "tin"]].fillna("").values)))
    check("C5: exactly one is_best per pair", (ad[ad.is_best].groupby([ad.npi.fillna(""), ad.tin]).size() == 1).all())

    # ---------------- C6 rate file
    rf = pd.read_parquet(P["stage6"] / "provider_rate_file.parquet")
    check("C6: required columns first", list(rf.columns[:6]) == ["NPI", "TIN", "Address", "Billing Code", "Specialty", "Rate"])
    check("C6: unique NPI x TIN x Address x Billing Code",
          not rf.fillna({"NPI": "", "Address": ""}).duplicated(["NPI", "TIN", "Address", "Billing Code"]).any(), f"{len(rf):,} rows")
    k = ["npi", "tin", "billing_code"]
    check("C6: every extracted (NPI,TIN,code) reaches the rate file",
          set(map(tuple, rates[k].fillna("").drop_duplicates().values))
          == set(map(tuple, rf[["NPI", "TIN", "Billing Code"]].fillna("").drop_duplicates().values)))
    r2 = rates.assign(_n=rates.npi.fillna(""))
    r2["pref"] = (r2.modifier != "").astype(int) * 4 + (~r2.service_code.str.split("|").map(lambda s: "11" in s)).astype(int) * 2
    modal = r2.groupby(["_n", "tin", "billing_code", "service_code"]).size().rename("c").reset_index() \
        .sort_values("c", ascending=False).drop_duplicates(["_n", "tin", "billing_code"])
    r2 = r2.merge(modal[["_n", "tin", "billing_code", "service_code"]].assign(m=1), how="left",
                  on=["_n", "tin", "billing_code", "service_code"])
    r2["pref"] += r2.m.isna().astype(int)
    exp = r2[r2.pref == r2.groupby(["_n", "tin", "billing_code"]).pref.transform("min")] \
        .groupby(["_n", "tin", "billing_code"]).rate.median().rename("exp").reset_index()
    got = rf.assign(_n=rf.NPI.fillna("")).drop_duplicates(["_n", "TIN", "Billing Code"])[["_n", "TIN", "Billing Code", "Rate"]]
    mm = exp.merge(got, left_on=["_n", "tin", "billing_code"], right_on=["_n", "TIN", "Billing Code"])
    check("C6: headline Rate = independent recomputation of rule D6.1", len(mm) == len(exp)
          and ((mm.exp - mm.Rate).abs() < 1e-9).all(), f"{len(mm):,} combinations")
    xl = pd.ExcelFile(P["stage6"] / "provider_rate_file.xlsx")
    check("C6: Excel has the 3 sheets and all rows", set(xl.sheet_names) == {"Rate File", "Rate Variants", "Read Me"}
          and len(pd.read_excel(xl, "Rate File", usecols=[0])) == len(rf))

    # ---------------- C7 labels follow thresholds
    ce = pd.read_parquet(P["stage7"] / "provider_cost_effectiveness.parquet")
    lab = pd.Series("AVERAGE", index=ce.index)
    lab[(ce.weighted_cost_index <= 0.90) & (ce.pct_codes_below_median >= 0.60)] = "COST_EFFECTIVE"
    lab[(ce.weighted_cost_index >= 1.10) & (ce.pct_codes_below_median <= 0.40)] = "HIGH_COST"
    lab[ce.n_codes_compared < 3] = "INSUFFICIENT_DATA"
    check("C7: labels follow the stated thresholds", (lab == ce.label).all(), f"{len(ce):,} providers")

    # ---------------- summary matches outputs
    S = json.load(open(P["raw"].parents[0] / "summary.json", encoding="utf-8"))
    vn = ad[ad.tier == "VERIFIED"].npi.dropna().nunique() / pairs.npi.dropna().nunique() * 100
    check("Summary: numbers match outputs", S["ppo_plans"] == int(labels.is_ppo.sum()) and S["final_rate_file"]["rows"] == len(rf)
          and S["rates_extracted"]["rate_rows"] == len(rates) and abs(S["address_linkage"]["pct_npis_with_verified_address"] - vn) < 0.06)

    # ---------------- repo rules
    src_text = "\n".join(p.read_text(encoding="utf-8") for p in (cfg["paths"]["raw"].parents[1] / "src").rglob("*.py"))
    check("Repo: no absolute Windows/home paths in src/", not re.search(r"[A-Za-z]:\\\\Users|/c/Users|C:/Users", src_text))

    fails = [r for r in RESULTS if not r[0]]
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
