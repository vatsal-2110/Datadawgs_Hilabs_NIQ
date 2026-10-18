"""Final C1 and C2 deliverables as Excel workbooks (generated from the pipeline outputs, never edited by hand).

  data/stage1/C1_index_plans_and_files.xlsx
      C1 Plan-File Table   brand, plan name, plan ID, market type, rate file URL (the PS table), one row per unique
                           (plan, file), with the scope decision and its third-party evidence
      Unique Rate Files    66 files after URL de-duplication
      Scope Evidence       per reporting structure: market, provider geography (NPPES), employer state (DOL Form 5500),
                           HIOS issuer (regulator filings), verdict
      Read Me
  data/stage2/C2_ppo_network.xlsx
      Plan Labels          every in-scope CA plan: product type, PPO yes/no, rule, reasoning, legal entity, regulator,
                           network, independent tests, evidence confidence
      PPO Plans            the 453 PPO plans and their rate file
      HIOS Products        test T2: one product type per regulator-filed product
      Employer Schedule A  test T3: employer-declared HMO / PPO contracts vs the plan labels
      Provider PPO Evidence  every (NPI, TIN) in the rate file: PPO_CONFIRMED / HMO_SIGNAL / MIXED / PPO_PRESUMED
      Product Types        what PPO / HMO / EOA / POS / EPO legally are, with quotes and source URLs
      Read Me

Usage: python -m src.export_c1_c2_excel
"""
from __future__ import annotations

import pandas as pd

from src.common.normalize import load_config


def write_book(path, sheets: dict, readme: list[tuple[str, str]]):
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for i, col in enumerate(df.columns, 1):
                width = max([len(str(col))] + [len(str(v)) for v in df[col].head(200).tolist()])
                ws.column_dimensions[ws.cell(1, i).column_letter].width = min(60, max(10, width + 2))
        pd.DataFrame(readme, columns=["Item", "Meaning"]).to_excel(xw, sheet_name="Read Me", index=False)
        xw.sheets["Read Me"].column_dimensions["A"].width = 26
        xw.sheets["Read Me"].column_dimensions["B"].width = 150


def c1_book(cfg):
    P = cfg["paths"]
    t = pd.read_parquet(P["stage1"] / "c1_plan_file_table.parquet")
    plans = pd.read_parquet(P["stage1"] / "plans.parquet")
    files = pd.read_parquet(P["stage1"] / "files.parquet")
    sev = pd.read_parquet(P["stage1"] / "scope_evidence.parquet")
    spl = pd.read_parquet(P["stage1"] / "scope_evidence_plans.parquet")

    key = plans.drop_duplicates("plan_key")[["plan_key", "plan_row_id", "structure_id"]]
    pe = key.merge(spl[["plan_row_id", "hios_issuer_entity", "sponsor_state", "sponsor_name"]], on="plan_row_id",
                   how="left").merge(sev[["structure_id", "verdict"]], on="structure_id", how="left")
    t = t.merge(pe.drop(columns="plan_row_id"), on="plan_key", how="left")
    t = t.rename(columns={"verdict": "scope_verdict_from_evidence",
                          "hios_issuer_entity": "hios_issuer_licensed_company",
                          "sponsor_state": "employer_state_dol_5500", "sponsor_name": "employer_name_dol_5500"})
    lead = ["brand", "plan_name", "plan_id", "plan_market_type", "rate_file_url", "file_role", "file_name",
            "in_scope", "scope_verdict_from_evidence", "scope_reason", "ca_evidence", "hios_issuer_licensed_company",
            "employer_state_dol_5500", "employer_name_dol_5500", "structure_id"]
    t = t[lead + [c for c in t.columns if c not in lead]].sort_values(["in_scope", "brand", "plan_name"],
                                                                       ascending=[False, True, True])
    f = files[["file_name", "file_role", "brands", "n_plan_rows", "n_in_scope_plans", "in_scope", "n_url_variants",
               "example_url", "file_key"]].sort_values(["in_scope", "n_plan_rows"], ascending=False)
    sv = sev.drop(columns=[c for c in ("c1_in_scope",) if c in sev]).sort_values("n_plans", ascending=False)

    readme = [
        ("What this is", "C1 deliverable: Centene's index files parsed into one table (brand, plan name, plan ID, market "
                         "type, rate file URL), duplicate files removed, and the California commercial group scope "
                         "proven with third-party evidence."),
        ("Index files", "11 Centene index files (Health Net, Ambetter, Fidelis, QualChoice + 7 QualChoice sub-indexes), "
                        "3,275 plan rows; 80 rate-file references -> 66 unique files (URL without signed query string)."),
        ("C1 Plan-File Table", "One row per unique (plan, rate file). Each plan lists an in-network file and an "
                               "allowed-amounts file. in_scope = CA commercial group."),
        ("scope_verdict_from_evidence", "Verdict of the reporting structure from independent tests (Scope Evidence "
                                        "sheet). Agrees with in_scope for every structure."),
        ("Scope Evidence: E1 market", "plan_market_type in the index (group / individual)."),
        ("Scope Evidence: E2 providers", "NPPES practice state of the providers in the structure's in-network file "
                                         "(Health Net CA: 96.6% CA; Health Net 'Oregon': 97.3% OR)."),
        ("Scope Evidence: E3 employers", "Group plan IDs are employer EINs, matched to US DOL Form 5500 / 5500-SF "
                                         "filings 2023-2024 (Health Net CA: 76.3% CA; 'Oregon': 99.2% OR; QualChoice: "
                                         "100% AR)."),
        ("Scope Evidence: E4 issuer", "HIOS issuer ID -> licensed company and state from CMS / state regulator "
                                      "filings (67138 Health Net of California; 10940 Health Net Oregon; 37903 / "
                                      "70525 QualChoice / QCA Arkansas; 25303 Fidelis NY)."),
        ("Result", "In scope: 1,312 Health Net CA plans in one structure, using one in-network file "
                   "(2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json) and one allowed-amounts "
                   "file."),
        ("Logic", "docs/logic_ppo_network.md section 1; report data/stage1/c1_scope_evidence.txt"),
    ]
    out = P["stage1"] / "C1_index_plans_and_files.xlsx"
    write_book(out, {"C1 Plan-File Table": t, "Unique Rate Files": f, "Scope Evidence": sv}, readme)
    return out, len(t)


def c2_book(cfg):
    P = cfg["paths"]
    lab = pd.read_parquet(P["stage2"] / "plan_labels.parquet")
    ev = pd.read_parquet(P["stage2"] / "plan_label_evidence.parquet")
    docs = cfg["evidence"]["product_types"]
    files = pd.read_parquet(P["stage1"] / "files.parquet")
    rate_file = files[files.in_scope & (files.file_role == "in_network")].file_name.iloc[0]

    # plan_labels already carries the final decision (decided_by / evidence / cross_checks / sources, written by
    # stage2_network_evidence); take the remaining test columns from plan_label_evidence
    lab = lab.drop(columns=["final_network_use", "evidence_confidence"], errors="ignore")
    pl = lab.merge(ev[["plan_row_id", "hios_product", "t2_product_pure", "sched_a_hmo", "sched_a_ppo",
                       "t3_employer_agrees", "t4_entity", "t4_regulator", "t4_network", "t4_source",
                       "evidence_confidence"]], on="plan_row_id", how="left")
    pred = pd.read_parquet(P["stage2"] / "plan_network_prediction.parquet")
    # the name-based uses_ppo_network columns are superseded by the evidence-based prediction
    pl = pl.drop(columns=["uses_ppo_network", "uses_ppo_network_incl_inferred"], errors="ignore")
    pl = pl.merge(pred[["plan_row_id", "final_network_use", "basis", "uses_ppo_network", "design_key",
                        "evidence_family", "evidence"]].rename(columns={"evidence": "design_document_evidence",
                                                                         "evidence_family": "family_from_evidence"}),
                  on="plan_row_id", how="left")
    pl["in_network_rate_file"] = rate_file
    pl = pl.rename(columns={"t4_entity": "legal_entity", "t4_regulator": "regulator", "t4_network": "network_documented",
                            "t4_source": "entity_source", "t2_product_pure": "T2_hios_product_single_type",
                            "sched_a_hmo": "T3_employer_declared_HMO", "sched_a_ppo": "T3_employer_declared_PPO",
                            "t3_employer_agrees": "T3_employer_agrees"})
    lead = ["plan_name", "plan_id", "plan_id_format", "final_network_use", "is_ppo_network", "decided_by",
            "evidence", "cross_checks", "sources", "basis", "uses_ppo_network", "family_from_evidence", "design_key",
            "design_document_evidence", "product_type", "is_ppo", "name_evidence", "provider_network", "rule",
            "confidence", "evidence_confidence", "legal_entity", "regulator", "network_documented",
            "hios_product", "T2_hios_product_single_type", "T3_employer_declared_HMO", "T3_employer_declared_PPO",
            "T3_employer_agrees", "entity_source", "source", "network_evidence", "in_network_rate_file"]
    pl = pl[lead + [c for c in pl.columns if c not in lead]].sort_values(
        ["final_network_use", "basis", "plan_name"])
    ppo = pl[pl.final_network_use.eq("PPO_NETWORK_PRIMARY")][
        ["plan_name", "plan_id", "plan_id_format", "decided_by", "evidence", "cross_checks", "sources",
         "evidence_confidence", "hios_product", "in_network_rate_file", "plan_row_id", "plan_key"]]
    cat = pd.read_parquet(P["stage2"] / "plan_design_catalog.parquet")[
        ["market", "title", "product", "design", "oop", "design_key", "family", "contract_form", "has_out_of_network",
         "hmo_ppo_tiers", "offered_by", "sbc_plan_type", "url"]]
    cat["url"] = cfg["evidence"]["healthnet_doc_base"] + cat.url.where(cat.url.str.startswith("/"), "")

    prod = ev.dropna(subset=["hios_product"]).groupby("hios_product").agg(
        product_types=("product_type", lambda s: "|".join(sorted(set(s)))), n_plans=("plan_row_id", "size"),
        single_type=("t2_product_pure", "first"), example_plan=("plan_name", "first")).reset_index()
    emp = ev[ev.plan_id_format.eq("EIN") & ev.sched_a_hmo.notna()].groupby("plan_id").agg(
        index_product_types=("product_type", lambda s: "|".join(sorted(set(s)))),
        declared_HMO=("sched_a_hmo", "first"), declared_PPO=("sched_a_ppo", "first"),
        n_plans=("plan_row_id", "size"), example_plan=("plan_name", "first")).reset_index().rename(
        columns={"plan_id": "employer_ein"})
    prov = pd.read_parquet(P["stage2"] / "provider_network_evidence.parquet")
    prov = prov[["npi", "tin", "ppo_network_evidence", "ppo_evidence_detail", "g_counties", "g_non_hmo_county",
                 "l_ppo_label", "c_capitation", "h_hmo_label", "has_rates", "business_names",
                 "business_name_labels", "billing_classes"]].sort_values(["ppo_network_evidence", "npi"])
    types = pd.DataFrame([{"product_type": k, **v} for k, v in docs.items()])

    nu = pl.final_network_use.value_counts().to_dict()
    nev = int((pl.basis == "EVIDENCE").sum())
    readme = [
        ("What this is", "C2 deliverable: which CA commercial group plans use the PPO network, predicted from evidence "
                         "that does not depend on the network word in the plan name; plus which providers in the "
                         "shared rate file are evidenced as PPO network."),
        ("Result", f"PPO network primary: {nu.get('PPO_NETWORK_PRIMARY', 0)} plans; PPO network as a tier (EOA, POS): "
                   f"{nu.get('PPO_NETWORK_TIER', 0)}; HMO network only: {nu.get('HMO_NETWORK_ONLY', 0)}; EPO network: "
                   f"{nu.get('EPO_NETWORK', 0)}. {nev} of {len(pl)} plans classified from evidence (plan design / "
                   f"own SBC / HSA design / network brand vs Health Net's 2026 plan documents); evidence disagrees "
                   f"with the plan name 0 times. PPO rate file: {rate_file} (shared by all CA plans)."),
        ("final_network_use / basis", "EVIDENCE = from Health Net 2026 plan documents (Design Catalog sheet); "
                                      "PRODUCT_DOCUMENT = POS / EPO product documents; NAME_ONLY = employer-custom or "
                                      "exchange plan with no public document, so the name's network word is used."),
        ("Design Catalog", "Every Health Net 2026 large-group plan overview and small-group SBC: product, design, and "
                           "what the document itself says: contract (Certificate of Insurance = PPO; Evidence of "
                           "Coverage = HMO/EOA), out-of-network benefit column (PPO only), HMO/PPO tiers (EOA only)."),
        ("Plan Labels: rule", "R1 EOA > R2 POS > R3 EPO > R4 HMO > R5 PPO > R6 Salud family = HMO (first match wins)."),
        ("Plan Labels: legal_entity", "PPO = Health Net Life Insurance Company (CA Dept. of Insurance, PPO network). "
                                      "HMO / EOA / POS = Health Net of California, Inc. (DMHC, HMO network). EPO = Health "
                                      "Net Life on the separate PureCare One EPO network. See Product Types sheet."),
        ("T2 HIOS product", "Small-group plans' HIOS product (first 10 characters of the plan ID) holds one product type; "
                            "all 9 products are single-type; 67138CA073 = all 34 small-group PPO plans."),
        ("T3 employer Schedule A", "Employer's US DOL Form 5500 Schedule A HMO / PPO boxes: employers with a PPO plan "
                                   "declared a PPO contract 82%; employers with only HMO / EOA / POS plans declared HMO "
                                   "97%."),
        ("evidence_confidence", "HIGH = name + an independent test agree; MEDIUM = name only (no test available); "
                                "REVIEW = an independent test disagrees (kept, listed)."),
        ("Provider PPO Evidence", "PPO_CONFIRMED = practises in a CA county with no Health Net HMO network (OPA: HMO 31 "
                                  "counties, PPO all of CA) or PPO group label; HMO_SIGNAL = capitation or IPA / "
                                  "Ambetter-HMO label; MIXED = both; PPO_PRESUMED = direct fee-for-service, no further "
                                  "evidence (priced like PPO_CONFIRMED: median per-code difference 0.000)."),
        ("Logic", "docs/logic_ppo_network.md sections 2-4; reports data/stage2/c2_network_evidence.txt and "
                  "c2_provider_network.txt"),
    ]
    out = P["stage2"] / "C2_ppo_network.xlsx"
    write_book(out, {"Plan Network Use": pl, "PPO Network Plans": ppo, "Design Catalog": cat,
                     "HIOS Products": prod, "Employer Schedule A": emp, "Provider PPO Evidence": prov,
                     "Product Types": types}, readme)
    return out, len(pl), int(pl.final_network_use.eq("PPO_NETWORK_PRIMARY").sum())


def main():
    cfg = load_config()
    for build in (c1_book, c2_book):
        try:
            res = build(cfg)
            print(f"wrote {res[0]} ({res[1]:,} rows)")
        except PermissionError as e:   # workbook open in Excel: report it, keep building the other one
            print(f"SKIPPED (file is open in another program, close it and re-run): {e.filename}")


if __name__ == "__main__":
    main()
