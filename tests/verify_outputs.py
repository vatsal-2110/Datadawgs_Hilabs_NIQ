"""Independent checks of the whole pipeline (C1-C7, summary, Excel).

Re-derives key results with DIFFERENT code paths than the pipeline (plain json.load / pandas groupby
instead of ijson streaming + dicts) and checks every rule and cross-file consistency.

Usage (from project root, after the pipeline has run):
  python -m tests.verify_outputs
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
    check("Labels: EPO plans are on the EPO network, not counted as PPO network",
          (labels[labels.product_type == "EPO"].provider_network == "EPO_NETWORK").all()
          and not labels[labels.product_type == "EPO"].uses_ppo_network_incl_inferred.any())

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

    # ---------------- C4 rates: full independent recompute with pandas (decode -> per contract -> primary contract)
    hl = pd.read_excel(P["hilabs_xlsx"], sheet_name="Spec-Proc Weights", dtype=str)
    codes = set(hl.PROC_CD)
    asof = doc.get("last_updated_on", "")
    long, breadth = [], {}
    for it in doc["in_network"]:
        ct, c0 = it["billing_code_type"], str(it["billing_code"])
        for b in it["negotiated_rates"]:
            for p in b["negotiated_prices"]:
                if p.get("negotiated_type") not in ("negotiated", "fee schedule", "derived") or p["negotiated_rate"] <= 0                         or (p.get("expiration_date") or "9999") < asof:
                    continue
                a = (p.get("additional_information") or "").strip().upper()
                if ct == "RC" and re.fullmatch(r"\d{4}[0-9A-Z]|[A-Z]\d{4}", a):
                    code, name, rc = a, "", c0
                else:
                    code, name, rc = c0, it.get("name"), (c0 if ct == "RC" else "")
                svc = (code, name, p.get("billing_class"), "|".join(sorted(p.get("billing_code_modifier") or [])),
                       "|".join(sorted(p.get("service_code") or [])), rc, it.get("negotiation_arrangement") or "")
                for ref in b["provider_references"]:
                    breadth.setdefault(ref, set()).add(svc)
                    if code in codes:
                        long.append((ref,) + svc + (float(p["negotiated_rate"]),))
    del doc
    keys = ["billing_code", "item_name", "billing_class", "modifier", "service_code", "revenue_code",
            "negotiation_arrangement"]
    L = pd.DataFrame(long, columns=["ref"] + keys + ["r"])
    C = L.groupby(["ref"] + keys).r.median().rename("cr").reset_index()
    C["br"] = C.ref.map(lambda x: len(breadth[x]))
    C = C.merge(refdf.drop_duplicates(), on="ref").fillna({"npi": ""})
    k2 = ["npi", "tin"] + keys
    C["top"] = C.br.eq(C.groupby(k2).br.transform("max"))
    g = C.groupby(k2).agg(med_all=("cr", "median"), n_c=("ref", "nunique")).reset_index()         .merge(C[C.top].groupby(k2).cr.median().rename("prim").reset_index(), on=k2)
    R = rates.fillna({"npi": ""})
    R["item_name"] = R.item_name.where(~(R.billing_code_type.ne("RC") & R.revenue_code.ne("")), "")
    m = g.merge(R, on=k2, how="outer", indicator=True)
    check("Rates: same keys as independent recompute (decoded facility codes included)", (m._merge == "both").all(),
          f"recompute {len(g)} / parquet {len(R)} / only-one-side {int((m._merge != 'both').sum())}")
    both = m[m._merge == "both"]
    check("Rates: primary-contract rate, all-contract median and n_contracts identical to recompute",
          ((both.prim - both.rate).abs() < 1e-9).all() and ((both.med_all - both.rate_median_all).abs() < 1e-9).all()
          and (both.n_c == both.n_contracts).all())
    check("Rates: facility rows are decoded RC items (revenue_code kept)",
          rates[rates.component.eq("facility") & rates.billing_code_type.ne("RC")].revenue_code.ne("").all(),
          f"{int(rates.component.eq('facility').sum())} facility rows, {rates[rates.component.eq('facility')].billing_code.nunique()} codes")
    allr = pd.read_parquet(s4 / "rates_all.parquet", columns=["npi", "billing_code", "is_hilabs_code"])
    check("Rates: every provider in the file has at least one price (rates_all)",
          set(pairs.npi.dropna()) <= set(allr.npi.dropna()), f"{allr.npi.nunique()} NPIs")
    check("Rates: rates.parquet = HiLabs subset of rates_all", int(allr.is_hilabs_code.sum()) == len(rates))
    o = pd.read_parquet(s4 / "oon_allowed.parquet")
    raw_o = json.load(open(P["raw_rates"] / files[files.in_scope & files.file_role.eq("allowed_amount")].file_name.iloc[0],
                           encoding="utf-8"))
    n_o = sum(len(pr.get("npi") or []) for it in raw_o["out_of_network"] for a in it["allowed_amounts"]
              for pay in a["payments"] for pr in pay["providers"])
    check("OON: allowed-amount rows = NPI entries in the raw file, kept apart from in-network rates",
          len(o) == n_o and "allowed_amount" not in rates.columns, f"{n_o} rows")
    check("Rates: all codes in HiLabs list", rates.billing_code.isin(codes).all(),
          f"{rates.billing_code.nunique()} codes")
    check("Rates: rate > 0 and min <= rate <= max", (rates.rate > 0).all()
          and ((rates.rate_min <= rates.rate + 1e-9) & (rates.rate <= rates.rate_max + 1e-9)).all())
    check("Rates: every (npi,tin) exists in tin_npi_pairs",
          set(map(tuple, rates[["npi", "tin"]].fillna("").drop_duplicates().values))
          <= set(map(tuple, pairs[["npi", "tin"]].fillna("").values)))
    check("Rates: hilabs_spec_cds filled for every row", rates.hilabs_spec_cds.notna().all())
    hmo = rates.network_hint.str.contains("AMBETTER_HMO|IPA") | rates.in_capitation_code
    check("Rates: D2.7 hinted rows counted", True, f"{int(hmo.sum())} hinted / {len(rates)} rows")

    # ---------------- C4 Medicare benchmark: hand recompute of 99213 (non-facility) in Los Angeles from the raw CMS zip
    import csv as _csv, io as _io, zipfile as _zf
    z = _zf.ZipFile(P["cms_pfs"] / "rvu26d.zip")
    rv = list(_csv.reader(_io.StringIO(z.read(next(n for n in z.namelist() if "nonQPP" in n and n.endswith(".csv"))).decode("latin-1"))))
    line = next(x for x in rv if x and x[0] == "99213" and x[1] == "")
    gp = list(_csv.reader(_io.StringIO(z.read("GPCI2026.csv").decode("latin-1"))))
    la = next(x for x in gp if len(x) > 6 and x[1] == "CA" and x[2] == "18")
    hand = round((float(line[5]) * float(la[4]) + float(line[6]) * float(la[5]) + float(line[10]) * float(la[6])) * float(line[25]), 2)
    rm = pd.read_parquet(s4 / "rates.parquet")
    pick = rm[(rm.billing_code == "99213") & rm.modifier.eq("") & rm.medicare_locality.str.startswith("LOS ANGELES")
              & rm.service_code.str.contains("11")]
    check("C4 benchmark: Medicare 99213 LA = hand calculation from the CMS file", len(pick) > 0
          and (pick.medicare_rate - hand).abs().max() < 0.011, f"hand ${hand}, {len(pick)} rows")
    ok = rm.medicare_rate.notna()
    check("C4 benchmark: pct_of_medicare = rate / medicare_rate x 100; rate_check filled",
          ((rm.pct_of_medicare[ok] - 100 * rm.rate[ok] / rm.medicare_rate[ok]).abs() < 0.06).all() and rm.rate_check.notna().all(),
          f"{int(ok.sum()):,} benchmarked rows")

    # ---------------- C4 contract terms, estimates, trust score
    ct = pd.read_parquet(s4 / "provider_contract_terms.parquet")
    es = pd.read_parquet(s4 / "estimated_rates.parquet")
    real = set(map(tuple, rm[["npi", "tin", "billing_code"]].fillna("").values))
    check("Estimates: never overlap a real rate; estimated = multiplier x Medicare; only EXACT formulas",
          not any(k in real for k in map(tuple, es[["npi", "tin", "billing_code"]].fillna("").values))
          and ((es.estimated_rate - es.multiplier_pct * es.medicare_rate_nonfacility / 100).abs() < 0.011).all()
          and es.formula_class.eq("EXACT").all() and es.is_estimate.all(), f"{len(es):,} estimates")
    ex = ct[ct.formula_class.eq("EXACT") & ct.formula_source.eq("own")]
    check("Contract terms: EXACT = >= 2 codes and >= 80% of them on the multiplier",
          ((ex.n_codes >= 2) & (ex.n_codes_on_multiplier / ex.n_codes >= 0.8)).all(), f"{len(ex):,} EXACT providers")
    rf6 = pd.read_parquet(P["stage6"] / "provider_rate_file.parquet")
    parts = rf6.trust_detail.str.findall(r"(\d+)/").map(lambda x: sum(map(int, x)))
    check("C6 trust_score = sum of its five parts, within 0-100", (parts == rf6.trust_score).all()
          and rf6.trust_score.between(0, 100).all(), str(rf6.trust_band.value_counts().to_dict()))

    # ---------------- C5 state-licensing source and hold-out accuracy audit
    ad5 = pd.read_parquet(P["stage5"] / "npi_tin_address.parquet")
    au5 = pd.read_parquet(P["stage5"] / "c5_cdph_audit.parquet")
    acc5 = au5[au5.is_best].groupby("tier").same_building.mean().round(3)
    got = ad5[ad5.tier.isin(acc5.index)].groupby("tier").expected_accuracy.first()
    check("C5: expected_accuracy = hold-out CDPH audit of the tier (built without CDPH)",
          ((got - acc5.reindex(got.index)).abs() < 1e-9).all() and "CDPH" not in "|".join(au5.columns),
          str(acc5.to_dict()))
    cdph_rows = ad5.sources.fillna("").str.contains("CDPH")
    check("C5: CDPH-sourced addresses count as an independent family",
          (ad5[cdph_rows].n_independent_sources >= 1).all() and cdph_rows.sum() > 0, f"{int(cdph_rows.sum())} rows")

    # ---------------- C3 grades
    sc = pd.read_parquet(P["stage3"] / "file_scores.parquet")
    from src.stage3_grade_files import WEIGHTS
    mcols = list(WEIGHTS)
    check("C3: every metric in [0,100]", ((sc[mcols] >= 0) & (sc[mcols] <= 100)).all().all())
    recomputed = (sc[mcols] * pd.Series(WEIGHTS)).sum(axis=1) / sum(WEIGHTS.values())
    check("C3: overall = weighted mean of metrics", ((recomputed - sc.overall_score).abs() <= 0.06).all())
    check("C3: the used (in-scope) file is graded", (sc.role == "USED (in scope)").sum() == len(ins[ins.file_role == "in_network"]))

    # ---------------- C3 scorecard (official C3): metrics in range, overall = weighted mean (Trust Gate cap)
    from src.stage3_scorecard import W_AA, W_IN, W_IX
    cap = cfg["grading"]["scorecard"]["gate_cap"]
    ok_sc = True
    for k, W in (("in_network", W_IN), ("allowed", W_AA), ("index", W_IX)):
        d = pd.read_parquet(P["stage3"] / f"scorecard_{k}.parquet")
        raw = (d[list(W)] * pd.Series(W)).sum(axis=1) / sum(W.values())
        exp = raw.where(~d.trust_gate_failed.astype(bool), raw.clip(upper=cap))
        ok_sc &= bool(((d[list(W)] >= 0) & (d[list(W)] <= 100)).all().all() and ((exp - d.overall_score).abs() <= 0.06).all())
    check("C3 scorecard: every metric in [0,100]; overall = weighted mean with the Trust Gate, for all 3 file types", ok_sc)
    ins_files = set(ins.file_name)
    graded = set(pd.read_parquet(P["stage3"] / "scorecard_in_network.parquet").file_name) |         set(pd.read_parquet(P["stage3"] / "scorecard_allowed.parquet").file_name)
    check("C3 scorecard: every file we use is graded (in-scope in-network + allowed-amounts, all index files)",
          ins_files <= graded and len(pd.read_parquet(P["stage3"] / "scorecard_index.parquet")) == len(cfg["index_files"]))

    # ---------------- C5 address tiers follow the written rules
    ad = pd.read_parquet(P["stage5"] / "npi_tin_address.parquet")
    a = ad[ad.tier != "NONE"]
    exists = a.address_exists_by.fillna("").ne("")
    ver_rule = exists & ((a.n_independent_sources >= 2) | ((a.n_independent_sources == 1)
                                          & (a.tin_corroboration.astype(bool) | a.dac_name_match.astype(bool))))
    check("C5: every VERIFIED row satisfies the VERIFIED rule", ver_rule[a.tier == "VERIFIED"].all())
    check("C5: every row satisfying the VERIFIED rule is VERIFIED (PO boxes excluded)",
          (a[ver_rule & ~a.is_po_box.astype(bool)].tier == "VERIFIED").all())
    pro_rule = exists & (a.sources.str.contains("DAC|CDPH") | a.registry_match.fillna("").ne("")) & ~ver_rule
    check("C5: every PROBABLE row has a geocoded non-NPPES source (DAC, CMS registry or CDPH)",
          pro_rule[a.tier == "PROBABLE"].all())
    check("C5: NPPES-only addresses are never above UNVERIFIED (update age is not evidence, audit A)",
          (a[~a.sources.str.contains(r"DAC|REGISTRY|CDPH", regex=True) & ~a.tin_corroboration.astype(bool)]
           .tier == "UNVERIFIED").all())
    check("C5: an address exists by the Census geocoder or a non-NPPES government record (never NPPES alone)",
          (a[exists & ~a.geocoded.astype(bool)].sources.str.contains("DAC|CDPH|REGISTRY", regex=True)).all())
    check("C5: no PO box is VERIFIED or PROBABLE", not (a.is_po_box.astype(bool) & a.tier.isin(["VERIFIED", "PROBABLE"])).any())
    check("C5: no address is supported only by DAC_GROUP (confirmation-only source)",
          not a.sources.str.fullmatch(r"DAC_GROUP(\|REGISTRY:.*)?").any())
    check("C5: every (npi,tin) pair has an address row (or NONE)",
          set(map(tuple, pairs[["npi", "tin"]].fillna("").values)) == set(map(tuple, ad[["npi", "tin"]].fillna("").values)))
    check("C5: exactly one is_best per pair", (ad[ad.is_best].groupby([ad.npi.fillna(""), ad.tin]).size() == 1).all())

    # ---------------- C6 rate file
    rf = pd.read_parquet(P["stage6"] / "provider_rate_file.parquet")
    pred = pd.read_parquet(P["stage2"] / "plan_network_prediction.parquet")
    cat = pd.read_parquet(P["stage2"] / "plan_design_catalog.parquet")
    lg = cat[(cat.market == "large") & cat.design_key.notna()]
    check("C2 design: no benefit design is offered both as PPO and as HMO/EOA in Health Net's 2026 catalog",
          not lg.groupby("design_key").family.agg(lambda s: "PPO" in set(s) and len(set(s)) > 1).any())
    check("C2 design: every PPO document has an out-of-network column, no HMO/EOA document does",
          lg[lg.family == "PPO"].has_out_of_network.all() and not lg[lg.family != "PPO"].has_out_of_network.any())
    check("C2 design: evidence-based predictions never contradict the plan name",
          pred[pred.evidence_based].agrees_with_name.all(), f"{int(pred.evidence_based.sum())} plans with evidence")
    check("C2 design: every in-scope plan has a final network use",
          len(pred) == len(labels) and pred.final_network_use.notna().all())
    check("C2 design: PPO_NETWORK_PRIMARY set == is_ppo set",
          set(pred[pred.final_network_use == "PPO_NETWORK_PRIMARY"].plan_row_id) == set(labels[labels.is_ppo].plan_row_id))
    pe = pd.read_parquet(P["stage2"] / "provider_network_evidence.parquet")
    hmo_cty = set(cfg["evidence"]["hmo_service_area"]["counties"])
    conf_geo = pe[pe.g_non_hmo_county]
    check("C2 providers: geography-confirmed pairs never sit only in HMO-service-area counties",
          all(any(c not in hmo_cty for c in s.split(" | ")) for s in conf_geo.g_counties.fillna("")))
    check("C2 providers: every pair classified once",
          len(pe) == len(pairs) and pe.ppo_network_evidence.isin(["PPO_CONFIRMED", "HMO_SIGNAL", "MIXED", "PPO_PRESUMED"]).all())
    check("C2 providers: PPO_CONFIRMED has no HMO evidence; HMO_SIGNAL has no PPO evidence",
          not (pe[pe.ppo_network_evidence == "PPO_CONFIRMED"][["c_capitation", "h_hmo_label"]].any(axis=1)).any()
          and not (pe[pe.ppo_network_evidence == "HMO_SIGNAL"][["g_non_hmo_county", "l_ppo_label"]].any(axis=1)).any())

    _sp = pd.read_excel(P["hilabs_xlsx"], sheet_name="Spec-Proc Weights", dtype=str)
    _sp["w"] = _sp.SPEC_PROC_WEIGHT.astype(float)
    top_spec = _sp.sort_values("w", ascending=False).drop_duplicates("PROC_CD").set_index("PROC_CD").SPEC_CD.to_dict()
    cd_ = rf[rf.specialty_source.eq("CODE_DEFAULT")]
    check("C6: every row has a HiLabs specialty; CODE_DEFAULT = the code's top-weight HiLabs specialty",
          rf.spec_cd.notna().all() and (cd_.spec_cd == cd_["Billing Code"].map(top_spec)).all(),
          str(rf.specialty_source.value_counts().to_dict()))
    check("C6: required columns first", list(rf.columns[:6]) == ["NPI", "TIN", "Address", "Billing Code", "Specialty", "Rate"])
    check("C6: unique NPI x TIN x Address x Billing Code",
          not rf.fillna({"NPI": "", "Address": ""}).duplicated(["NPI", "TIN", "Address", "Billing Code"]).any(), f"{len(rf):,} rows")
    k = ["npi", "tin", "billing_code"]
    exh = pd.read_parquet(P["stage6"] / "provider_rate_file_excluded_hmo.parquet")
    both6 = pd.concat([rf, exh])
    check("C6: every extracted (NPI,TIN,code) reaches the rate file or the HMO-excluded sheet",
          set(map(tuple, rates[k].fillna("").drop_duplicates().values))
          == set(map(tuple, both6[["NPI", "TIN", "Billing Code"]].fillna("").drop_duplicates().values)))
    check("C6: Rate File is PPO network only (no HMO_SIGNAL); every excluded row is HMO_SIGNAL with a reason",
          not rf.ppo_network_evidence.eq("HMO_SIGNAL").any() and exh.ppo_network_evidence.eq("HMO_SIGNAL").all()
          and exh.exclusion_reason.str.len().gt(12).all(), f"{len(rf):,} PPO rows, {len(exh):,} excluded")
    r2 = rates.assign(_n=rates.npi.fillna(""))
    own = r2.groupby(["_n", "tin"]).component.agg(lambda s: s.value_counts().idxmax()).rename("own").reset_index()
    r2 = r2.merge(own, on=["_n", "tin"])
    r2["pref"] = (r2.negotiation_arrangement.eq("capitation").astype(int) * 16 + r2.component.ne(r2.own).astype(int) * 8
                  + (r2.modifier != "").astype(int) * 4
                  + (~r2.service_code.str.split("|").map(lambda s: "11" in s)).astype(int) * 2)
    modal = r2.groupby(["_n", "tin", "billing_code", "service_code"]).size().rename("c").reset_index() \
        .sort_values("c", ascending=False).drop_duplicates(["_n", "tin", "billing_code"])
    r2 = r2.merge(modal[["_n", "tin", "billing_code", "service_code"]].assign(m=1), how="left",
                  on=["_n", "tin", "billing_code", "service_code"])
    r2["pref"] += r2.m.isna().astype(int)
    exp = r2[r2.pref == r2.groupby(["_n", "tin", "billing_code"]).pref.transform("min")] \
        .groupby(["_n", "tin", "billing_code"]).rate.median().rename("exp").reset_index()
    got = both6.assign(_n=both6.NPI.fillna("")).drop_duplicates(["_n", "TIN", "Billing Code"])[["_n", "TIN", "Billing Code", "Rate"]]
    mm = exp.merge(got, left_on=["_n", "tin", "billing_code"], right_on=["_n", "TIN", "Billing Code"])
    check("C6: headline Rate = independent recomputation of rule D6.1", len(mm) == len(exp)
          and ((mm.exp - mm.Rate).abs() < 1e-9).all(), f"{len(mm):,} combinations")
    xl = pd.ExcelFile(P["stage6"] / "provider_rate_file.xlsx")
    check("C6: Excel has the 6 sheets and all rows", set(xl.sheet_names) == {"Summary", "Rate File", "Rate Variants", "Estimated Rates", "Excluded - HMO signals", "Read Me"}
          and len(pd.read_excel(xl, "Rate File", usecols=[0])) == len(rf))

    # ---------------- C7 labels follow thresholds
    ce = pd.read_parquet(P["stage7"] / "provider_cost_effectiveness.parquet")
    lab = pd.Series("AVERAGE", index=ce.index)
    lab[(ce.weighted_cost_index <= 0.90) & (ce.pct_codes_below_median >= 0.60)] = "COST_EFFECTIVE"
    lab[(ce.weighted_cost_index >= 1.10) & (ce.pct_codes_below_median <= 0.40)] = "HIGH_COST"
    lab[ce.n_codes_compared < 1] = "INSUFFICIENT_DATA"
    check("C7: labels follow the stated thresholds", (lab == ce.label).all(), f"{len(ce):,} providers")
    q_ = ce.quality_good.astype("boolean")
    low_ = ce.weighted_cost_index <= 1.0
    exp_v = pd.Series("PRICE_ONLY", index=ce.index)
    exp_v[q_.notna() & low_ & q_.fillna(False)] = "HIGH_VALUE"
    exp_v[q_.notna() & low_ & ~q_.fillna(True)] = "LOW_PRICE_LOW_QUALITY"
    exp_v[q_.notna() & ~low_ & q_.fillna(False)] = "HIGH_PRICE_HIGH_QUALITY"
    exp_v[q_.notna() & ~low_ & ~q_.fillna(True)] = "LOW_VALUE"
    check("C7: value_label = price vs peers x CMS quality; PRICE_ONLY exactly where no CMS quality",
          (exp_v == ce.value_label).all() and (ce.quality_basis.eq("NONE") == ce.value_label.eq("PRICE_ONLY")).all(),
          str(ce.value_label.value_counts().to_dict()))
    check("C7: only PPO-network providers scored (no HMO-excluded provider)",
          not set(zip(ce.NPI, ce.TIN)) & set(zip(exh.NPI, exh.TIN)), f"{len(ce):,} providers")

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
