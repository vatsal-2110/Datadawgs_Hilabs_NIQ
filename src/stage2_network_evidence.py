"""C2 evidence: is each in-scope plan really PPO (or HMO / EOA / POS / EPO)? Tested beyond the plan name.

C2 layer 1 (src.stage2_plan_labels) labels every plan from the network word in its name. This module checks those
labels against evidence that does NOT come from the name:

  T2 regulator product   Small-group plans carry HIOS plan IDs. The first 10 characters are the HIOS *product*
                         (e.g. 67138CA073): plans filed with regulators as one product share one benefit/network
                         design. Test: does every HIOS product contain a single product type?      [CMS HIOS]
  T3 employer filing     Large-group plan IDs are employer EINs. Each employer's Form 5500 Schedule A lists its
                         insurance contracts and ticks the benefit type (HMO / PPO). Test: employers whose Health
                         Net plans in the index include a PPO should declare a PPO contract; employers with only
                         HMO / EOA plans should not.                                                 [US DOL EBSA]
  T4 legal entity        What each product type legally IS: issuing company, regulator and provider network, quoted
                         from Health Net documents and California regulator filings (config evidence.product_types).
                         PPO = Health Net Life (CDI, PPO network); HMO / EOA / POS = Health Net of California (DMHC,
                         HMO network); EPO = Health Net Life on the separate PureCare One EPO network.

Per plan: evidence_confidence = HIGH (name + an independent test agree), MEDIUM (name only, no test available),
REVIEW (an independent test disagrees; printed for review, label not changed silently).

Outputs (data/stage2/): plan_label_evidence.parquet (+ .csv), c2_network_evidence.txt
Usage: python -m src.stage2_network_evidence
"""
from __future__ import annotations

import io
import zipfile

import pandas as pd

from src.common.normalize import load_config, save_csv

NOT_HN = r"NETWORK|VERITY|FIRST CHOICE|UPMC|COMMUNITY"   # carriers whose names merely contain "health net..."


def load_schedule_a(cfg) -> pd.DataFrame:
    """Health Net insurance contracts in DOL Form 5500 Schedule A, aggregated per employer EIN."""
    parts = []
    for z in sorted(cfg["paths"]["dol"].glob("F_SCH_A_*_Latest.zip")):
        with zipfile.ZipFile(z) as zf:
            csv = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            parts.append(pd.read_csv(io.BytesIO(zf.read(csv)), dtype=str, encoding="latin-1",
                                     usecols=["SCH_A_EIN", "INS_CARRIER_NAME", "INS_CARRIER_NAIC_CODE",
                                              "WLFR_BNFT_HMO_IND", "WLFR_BNFT_PPO_IND"]))
    if not parts:
        return pd.DataFrame(columns=["ein", "sched_a_hmo", "sched_a_ppo", "sched_a_carriers"])
    a = pd.concat(parts, ignore_index=True)
    name = a.INS_CARRIER_NAME.fillna("").str.upper()
    a = a[name.str.contains(r"HEALTH ?NET", regex=True) & ~name.str.contains(NOT_HN, regex=True)].copy()
    a["ein"] = a.SCH_A_EIN.str.replace(r"\D", "", regex=True).str.zfill(9)
    return a.groupby("ein").agg(
        sched_a_hmo=("WLFR_BNFT_HMO_IND", lambda s: (s == "1").any()),
        sched_a_ppo=("WLFR_BNFT_PPO_IND", lambda s: (s == "1").any()),
        sched_a_carriers=("INS_CARRIER_NAME", lambda s: " | ".join(sorted(set(s.str.upper())))[:120])).reset_index()


DECISION_COLS = ["final_network_use", "is_ppo_network", "decided_by", "evidence", "cross_checks", "sources",
                 "name_evidence", "evidence_confidence"]
FACTOR = {"D": "PLAN_DESIGN (Health Net current plan-year overview)", "M": "PLAN_DESIGN (earlier-year design)",
          "M2": "PLAN_DESIGN (partial design)", "S": "OWN_SBC (Summary of Benefits and Coverage)",
          "A": "HSA_DESIGN", "N": "NETWORK_BRAND"}
NETWORK_TEXT = {
    "PPO_NETWORK_PRIMARY": "uses the Health Net PPO Network as its network (in- and out-of-network benefits)",
    "PPO_NETWORK_TIER": "HMO-family plan whose members also reach the PPO network through a PPO tier; not a PPO plan",
    "HMO_NETWORK_ONLY": "uses an HMO network only; not PPO",
    "EPO_NETWORK": "uses the separate PureCare One EPO network; not PPO",
}


def write_decisions(cfg, lab: pd.DataFrame):
    """Final C2 decision per plan with ALL evidence written out: plan_labels.csv / .parquet (columns replaced in place:
    `evidence` now explains the decision, the original name reasoning moves to `name_evidence`) and the submission
    file ppo_network_decisions.csv."""
    P = cfg["paths"]
    pred = pd.read_parquet(P["stage2"] / "plan_network_prediction.parquet")
    cat = pd.read_parquet(P["stage2"] / "plan_design_catalog.parquet")
    base = cfg["evidence"]["healthnet_doc_base"]
    cat["full_url"] = base + cat.url
    doc_by_key = cat[cat.market == "large"].dropna(subset=["design_key"]).groupby("design_key").full_url.first()
    doc_by_sbc = cat[cat.market == "small"].drop_duplicates("sbc_name").set_index("sbc_name").full_url
    pages = cfg["evidence"]["healthnet_broker_pages"]
    product_src = {k: v["source"] for k, v in cfg["evidence"]["product_types"].items()}
    pdocs = cfg["evidence"]["product_documents"]
    files = pd.read_parquet(P["stage1"] / "files.parquet")
    rate_files = " | ".join(files[files.in_scope & (files.file_role == "in_network")].file_name)

    d = lab.merge(pred[["plan_row_id", "final_network_use", "basis", "evidence", "design_key"]]
                  .rename(columns={"evidence": "pred_evidence"}), on="plan_row_id", how="left")
    rows = []
    for r in d.itertuples():
        code = (r.pred_evidence or "").split(":", 1)[0].strip() if isinstance(r.pred_evidence, str) else ""
        parts, srcs = [], []
        # 1) the deciding factor
        if r.basis == "EVIDENCE" and code in FACTOR:
            decided_by = FACTOR[code]
            parts.append(r.pred_evidence.split(" | ")[0])
            if code == "D" and r.design_key in doc_by_key.index:
                srcs.append(doc_by_key[r.design_key])
            elif code == "S" and r.plan_name in doc_by_sbc.index:
                srcs.append(doc_by_sbc[r.plan_name])
            else:
                srcs.append(pages["large"] if code != "S" else pages["small"])
        elif r.basis == "PRODUCT_DOCUMENT":
            decided_by = "PRODUCT_DOCUMENT"
            doc = pdocs["POS" if r.product_type == "POS" else "EPO"]
            parts.append(f"{r.product_type} product document: {doc['text']}")
            srcs.append(doc["url"])
        else:
            decided_by = "PLAN_NAME_ONLY (no public plan document for this employer-custom / exchange plan)"
            parts.append(f"no public Health Net plan document for this plan; network word in the name used: {r.evidence}")
        # 2) what the network use means, with the product definition
        parts.append(f"result: {NETWORK_TEXT.get(r.final_network_use, r.final_network_use)}")
        if product_src.get(r.product_type):
            srcs.append(product_src[r.product_type])
        # 3) independent cross-checks (not used to decide)
        checks = []
        if pd.notna(r.t2_product_pure):
            checks.append(f"HIOS product {r.hios_product} (regulator filing) contains only {r.t2_product_types} plans")
        if pd.notna(r.flagged) and r.flagged:
            declared = "+".join(x for x, f in (("HMO", r.sched_a_hmo), ("PPO", r.sched_a_ppo)) if f)
            checks.append(f"employer's DOL Form 5500 Schedule A declares a Health Net {declared} contract"
                          + ("" if r.t3_employer_agrees is None else
                             (" (agrees)" if r.t3_employer_agrees else " (box under-ticked; review)")))
        checks.append(f"plan name word: {r.product_type} ("
                      + ("agrees" if (r.final_network_use == "PPO_NETWORK_PRIMARY") == (r.product_type == "PPO")
                         else "differs") + ")")
        rows.append({"plan_row_id": r.plan_row_id, "decided_by": decided_by, "evidence": " | ".join(parts),
                     "cross_checks": " | ".join(checks), "sources": " ; ".join(dict.fromkeys(s for s in srcs if s))})
    ev = pd.DataFrame(rows)
    d = d.merge(ev, on="plan_row_id", suffixes=("_name", ""))
    d["name_evidence"] = d["evidence_name"]
    d["is_ppo_network"] = d.final_network_use.eq("PPO_NETWORK_PRIMARY")

    # plan_labels: keep every original column, replace `evidence`, add the decision columns
    new = ["final_network_use", "is_ppo_network", "decided_by", "evidence", "cross_checks", "sources", "name_evidence"]
    pl = lab[[c for c in lab.columns if c != "evidence"]].merge(d[["plan_row_id"] + new], on="plan_row_id", how="left")
    pl = pl.drop(columns=[c for c in ("hios_product", "t2_product_pure", "t2_product_types", "sched_a_hmo",
                                      "sched_a_ppo", "flagged", "emp_types", "t3_employer_agrees", "t4_entity",
                                      "t4_regulator", "t4_network", "t4_source") if c in pl])
    front = ["plan_row_id", "plan_name", "plan_id", "plan_id_format", "final_network_use", "is_ppo_network",
             "decided_by", "evidence", "cross_checks", "sources", "evidence_confidence", "product_type", "is_ppo",
             "name_evidence"]
    pl = pl[front + [c for c in pl.columns if c not in front]]
    pl.to_parquet(P["stage2"] / "plan_labels.parquet", index=False)
    save_csv(pl, P["stage2"] / "plan_labels.csv")

    sub = pl[["plan_name", "plan_id", "plan_id_format", "final_network_use", "is_ppo_network", "decided_by",
              "evidence", "cross_checks", "sources", "evidence_confidence", "plan_row_id"]].copy()
    sub["in_network_rate_file"] = rate_files   # from C1 files.parquet, not typed in
    sub = sub.sort_values(["is_ppo_network", "final_network_use", "plan_name"], ascending=[False, True, True])
    save_csv(sub, P["stage2"] / "ppo_network_decisions.csv")
    sub.to_parquet(P["stage2"] / "ppo_network_decisions.parquet", index=False)
    print(f"wrote plan_labels.csv and ppo_network_decisions.csv: decided by {sub.decided_by.value_counts().to_dict()}")


def main():
    cfg = load_config()
    P = cfg["paths"]
    lab = pd.read_parquet(P["stage2"] / "plan_labels.parquet")
    if "name_evidence" in lab:            # re-run: restore the name-only columns written by stage2_plan_labels
        lab["evidence"] = lab["name_evidence"]
        lab = lab.drop(columns=[c for c in DECISION_COLS if c in lab and c != "evidence"])
    docs = cfg["evidence"]["product_types"]

    # ---- T2: HIOS product purity
    lab["hios_product"] = lab.plan_id.where(lab.plan_id.str.match(r"^\d{5}[A-Z]{2}\d{3}"), None).str[:10]
    prod = lab.dropna(subset=["hios_product"]).groupby("hios_product").agg(
        n_plans=("plan_row_id", "size"), product_types=("product_type", lambda s: "|".join(sorted(set(s)))),
        example=("plan_name", "first")).reset_index()
    prod["pure"] = ~prod.product_types.str.contains(r"\|")
    lab = lab.merge(prod[["hios_product", "pure", "product_types"]].rename(
        columns={"pure": "t2_product_pure", "product_types": "t2_product_types"}), on="hios_product", how="left")

    # ---- T3: employer Schedule A declarations
    sa = load_schedule_a(cfg)
    emp = lab[lab.plan_id_format == "EIN"].groupby("plan_id").agg(
        emp_has_ppo_plan=("product_type", lambda s: (s == "PPO").any()),
        emp_types=("product_type", lambda s: "|".join(sorted(set(s))))).reset_index()
    emp = emp.merge(sa, left_on="plan_id", right_on="ein", how="inner")
    emp["flagged"] = emp.sched_a_hmo | emp.sched_a_ppo
    lab = lab.merge(emp[["plan_id", "sched_a_hmo", "sched_a_ppo", "flagged", "emp_types"]], on="plan_id", how="left")

    def t3(r):
        if pd.isna(r.flagged) or not r.flagged:
            return None
        if r.product_type == "PPO":
            return bool(r.sched_a_ppo)          # a PPO plan needs a PPO contract
        if r.product_type in ("HMO", "EOA_HYBRID", "POS") and "PPO" not in str(r.emp_types).split("|"):
            return bool(r.sched_a_hmo)          # employer with no PPO plan should declare HMO
        return None                             # mixed employer: can't attribute the flags to one plan
    lab["t3_employer_agrees"] = [t3(r) for r in lab.itertuples()]

    # ---- T4: documented entity / regulator / network
    for k in ("entity", "regulator", "network", "source"):
        lab[f"t4_{k}"] = lab.product_type.map(lambda t: docs.get(t, {}).get(k))

    # ---- confidence
    def conf(r):
        tests = [x for x in (r.t2_product_pure if pd.notna(r.t2_product_pure) else None, r.t3_employer_agrees)
                 if x is not None]
        if any(t is False for t in tests):
            return "REVIEW"   # an independent test disagrees; label kept, listed for review
        return "HIGH" if tests else "MEDIUM"
    lab["evidence_confidence"] = [conf(r) for r in lab.itertuples()]
    cols = ["plan_row_id", "plan_name", "plan_id", "plan_id_format", "product_type", "is_ppo", "rule", "evidence",
            "hios_product", "t2_product_pure", "t2_product_types", "sched_a_hmo", "sched_a_ppo", "emp_types",
            "t3_employer_agrees", "t4_entity", "t4_regulator", "t4_network", "t4_source", "evidence_confidence"]
    out = lab[cols]
    out.to_parquet(P["stage2"] / "plan_label_evidence.parquet", index=False)
    save_csv(out, P["stage2"] / "plan_label_evidence.csv")
    write_decisions(cfg, lab)

    # ---- report
    no_ppo = emp[~emp.emp_has_ppo_plan & emp.flagged]
    has_ppo = emp[emp.emp_has_ppo_plan & emp.flagged]
    L = ["C2 evidence: plan labels tested against evidence that is not the plan name", "=" * 88,
         f"T2 HIOS products: {len(prod)} products, {int(prod.pure.sum())} contain a single product type",
         *[f"   {r.hios_product}  {r.product_types:<10} {r.n_plans:>3} plans  e.g. '{r.example}'" for r in prod.itertuples()],
         "",
         f"T3 employer Form 5500 Schedule A: {len(sa)} employers hold a Health Net insurance contract; "
         f"{len(emp)} of them have plans in the CA index; {int(emp.flagged.sum())} ticked HMO and/or PPO",
         f"   employers WITH a PPO plan in the index ({len(has_ppo)}): declared a PPO contract "
         f"{int(has_ppo.sched_a_ppo.sum())} ({100 * has_ppo.sched_a_ppo.mean():.0f}%)" if len(has_ppo) else "",
         f"   employers with ONLY HMO / EOA / POS plans ({len(no_ppo)}): declared HMO {int(no_ppo.sched_a_hmo.sum())} "
         f"({100 * no_ppo.sched_a_hmo.mean():.0f}%), PPO-only {int((no_ppo.sched_a_ppo & ~no_ppo.sched_a_hmo).sum())}"
         if len(no_ppo) else "",
         "",
         "T4 what each product type is (Health Net documents, CA regulator filings):",
         *[f"   {t:<11} {d['entity']} | {d['regulator']} | network: {d['network']}" for t, d in docs.items()],
         "",
         "Per-plan evidence confidence:",
         pd.crosstab(out.product_type, out.evidence_confidence).to_string(),
         "",
         "REVIEW (an independent test disagrees with the name label; all are employers with both HMO and PPO plans "
         "that ticked only one Schedule A box, or an EOA employer that ticked PPO for the EOA PPO tier):",
         out[out.evidence_confidence == "REVIEW"][["plan_name", "plan_id", "product_type", "t2_product_types",
                                                     "sched_a_hmo", "sched_a_ppo", "emp_types"]].to_string(index=False)
         or "   none"]
    (P["stage2"] / "c2_network_evidence.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
