"""C6: build the provider rate file at NPI x TIN x address x billing code level (deliverable 1).

Inputs : data/stage4/rates.parquet, data/stage5/npi_tin_address.parquet, data/stage5/npi_specialty.parquet,
         data/stage5/nppes_npi.parquet
Outputs: data/stage6/provider_rate_file.xlsx  sheets: "Rate File", "Rate Variants", "Read Me"
         data/stage6/provider_rate_file.parquet (+ .csv)

Headline rate per NPI x TIN x code (logic D6.1); C4 already chose one rate per service (primary contract):
  0. never a capitation row while a fee-for-service rate exists (capitation is an HMO payment model)
  1. the provider's own billing component: professional for a pair that bills mostly professional, facility
     (institutional, decoded from revenue-code items) for a pair that bills mostly facility
  2. prefer rows with NO modifier (standard/global service; 95/GT telehealth, 26/TC components are variants)
  3. among them prefer place of service containing 11 (office), else the most common place of service
  4. if several remain, median of their rates
All variants stay in the "Rate Variants" sheet.

Addresses per NPI x TIN (logic D6.2): every VERIFIED and PROBABLE address; if the pair has none, its single
best address (UNVERIFIED / NONE) so no rate is dropped. The tier is a column.

Usage: python -m src.stage6_build_rate_file
"""
from __future__ import annotations

import pandas as pd

from src.common.normalize import load_config, save_csv


def headline(rates: pd.DataFrame) -> pd.DataFrame:
    r = rates.copy()
    r["no_mod"] = r.modifier.eq("")
    r["office"] = r.service_code.str.split("|").map(lambda s: "11" in s)
    r["_npi"] = r.npi.fillna("")
    dom = r.groupby(["_npi", "tin"]).component.agg(lambda s: s.value_counts().idxmax())
    r["own_component"] = r.set_index(["_npi", "tin"]).index.map(dom).values == r.component.values
    r["cap"] = r.negotiation_arrangement.eq("capitation")
    kk = ["_npi", "tin", "billing_code"]
    pos_mode = r.groupby(kk + ["service_code"]).size().reset_index(name="n").sort_values("n", ascending=False) \
        .drop_duplicates(kk).set_index(kk).service_code
    r["is_mode_pos"] = r.set_index(kk).index.map(pos_mode).values == r.service_code.values
    # preference rank: no modifier first, then office, then modal place of service
    r["pref"] = (r.cap.astype(int) * 16 + (~r.own_component).astype(int) * 8 + (~r.no_mod).astype(int) * 4
                 + (~r.office).astype(int) * 2 + (~r.is_mode_pos).astype(int))
    best_pref = r.groupby(kk).pref.transform("min")
    chosen = r[r.pref == best_pref]
    agg = chosen.groupby(kk).agg(rate=("rate", "median"), place_of_service=("service_code", lambda s: "; ".join(sorted(set(s)))),
                                 modifier=("modifier", lambda s: "; ".join(sorted(set(s))) or ""),
                                 item_name=("item_name", "first"), rate_component=("component", "first"),
                                 revenue_code=("revenue_code", lambda s: "; ".join(sorted({x for x in s if x}))),
                                 rate_source=("rate_source", "first"), n_contracts=("n_contracts", "max"),
                                 billing_code_type=("billing_code_type", "first"), hilabs_spec_cds=("hilabs_spec_cds", "first"),
                                 network_hint=("network_hint", lambda s: "|".join(sorted({x for v in s for x in v.split("|") if x}))),
                                 in_capitation_code=("cap", "all"),
                                 medicare_rate=("medicare_rate", "median"), medicare_source=("medicare_source", "first"),
                                 rate_check=("rate_check", lambda s: "; ".join(sorted({x for v in s for x in v.split("; ")} - {"OK"})) or "OK"),
                                 contracts_agree=("n_distinct_rates", lambda s: bool((s <= 1).all())),
                                 contract_multiplier_pct=("contract_multiplier_pct", "first"),
                                 fits_contract_formula=("fits_contract_formula",
                                                        lambda s: None if s.isna().all() else bool(s.dropna().all()))
                                 ).reset_index()
    agg["pct_of_medicare"] = (100 * agg.rate / agg.medicare_rate).round(1)
    var = r.groupby(kk).agg(n_rate_variants=("rate", "size"), variant_rate_min=("rate", "min"),
                            variant_rate_max=("rate", "max")).reset_index()
    out = agg.merge(var, on=kk)
    out["npi"] = out._npi.replace("", None)
    return out.drop(columns="_npi")


def main():
    cfg = load_config()
    s4, s5, s6 = cfg["paths"]["stage4"], cfg["paths"]["stage5"], cfg["paths"]["stage6"]
    s6.mkdir(parents=True, exist_ok=True)
    rates = pd.read_parquet(s4 / "rates.parquet")
    addr = pd.read_parquet(s5 / "npi_tin_address.parquet")
    spec = pd.read_parquet(s5 / "npi_specialty.parquet")
    nppes = pd.read_parquet(s5 / "nppes_npi.parquet")[["npi", "enumeration_type", "name"]]

    h = headline(rates)
    good = addr.tier.isin(["VERIFIED", "PROBABLE"])
    pair_has_good = good.groupby([addr.npi.fillna(""), addr.tin]).transform("any")
    # every VERIFIED/PROBABLE address; pairs without one keep exactly their best (UNVERIFIED / NONE) address
    a = addr[good | (~pair_has_good & addr.is_best)]
    a = a[["npi", "tin", "address", "city", "state", "zip", "county_fips5", "lat", "lon", "tier", "expected_accuracy",
           "sources", "evidence"]].rename(columns={"expected_accuracy": "address_expected_accuracy"})
    for d in (h, a):
        d["_npi"] = d.npi.fillna("")
    f = h.merge(a.drop(columns="npi"), on=["_npi", "tin"], how="left")
    f["_npi"] = f._npi.replace("", None)
    f = f.merge(spec[["npi", "spec_cd", "specialty", "specialty_source", "primary_taxonomy_desc"]], left_on="_npi", right_on="npi",
                how="left", suffixes=("", "_s")).drop(columns=["npi_s"])
    f = f.merge(nppes.rename(columns={"npi": "_npi", "enumeration_type": "npi_type", "name": "provider_name"}),
                on="_npi", how="left")
    f["npi"] = f["_npi"]
    f["specialty"] = f.specialty.where(f.specialty.notna(), f.npi.map(
        lambda n: "Unmapped (TIN-only contract, no NPI)" if n is None or pd.isna(n) else "Unmapped (NPI not in NPPES)"))
    f["specialty_source"] = f.specialty_source.fillna("UNMAPPED")
    f["provider_taxonomy"] = f.primary_taxonomy_desc
    # D6.3: a provider with no HiLabs specialty (hospital, generic clinic, taxonomy HiLabs doesn't list) gets, per row,
    # the HiLabs specialty that owns the billing code (highest SPEC_PROC_WEIGHT for that code), labelled CODE_DEFAULT;
    # its own taxonomy stays in provider_taxonomy, so nothing is hidden
    sp = pd.read_excel(cfg["paths"]["hilabs_xlsx"], sheet_name="Spec-Proc Weights", dtype=str)
    sp["w"] = sp.SPEC_PROC_WEIGHT.astype(float)
    top = sp.sort_values("w", ascending=False).drop_duplicates("PROC_CD").set_index("PROC_CD")
    um = f.specialty_source.eq("UNMAPPED")
    f.loc[um, "spec_cd"] = f.loc[um, "billing_code"].map(top.SPEC_CD)
    f.loc[um, "specialty"] = f.loc[um, "billing_code"].map(top.SPEC_DESCRIPTION)
    f.loc[um & f.spec_cd.notna(), "specialty_source"] = "CODE_DEFAULT"
    f["code_in_specialty_list"] = [bool(s) and isinstance(c, str) and s in c.split(",")
                                   for s, c in zip(f.spec_cd, f.hilabs_spec_cds)]
    f["tier"] = f.tier.fillna("NONE")
    # provider-level PPO-network evidence (src.stage2_provider_network): lets users keep only proven PPO contracts
    pe = cfg["paths"]["stage2"] / "provider_network_evidence.parquet"
    if pe.exists():
        pe = pd.read_parquet(pe)[["npi", "tin", "ppo_network_evidence", "ppo_evidence_detail"]]
        f = f.assign(_k=f.npi.fillna("")).merge(pe.assign(_k=pe.npi.fillna("")).drop(columns="npi"),
                                                on=["_k", "tin"], how="left").drop(columns="_k")
    else:
        f["ppo_network_evidence"], f["ppo_evidence_detail"] = None, None
    # trust score (0-100): how far to trust this row's rate, from five independent checks (logic D6.4)
    pts = {"check": f.rate_check.eq("OK") * 30,
           "contracts": f.contracts_agree.map({True: 15, False: 5}).fillna(5),
           "formula": f.fits_contract_formula.map({True: 20, False: 0}).fillna(10),
           "ppo": f.ppo_network_evidence.map({"PPO_CONFIRMED": 20, "PPO_PRESUMED": 12, "MIXED": 8, "HMO_SIGNAL": 0}).fillna(8),
           "address": f.tier.map({"VERIFIED": 15, "PROBABLE": 10, "UNVERIFIED": 4, "NONE": 0}).fillna(0)}
    f["trust_score"] = sum(pts.values()).astype(int)
    f["trust_band"] = pd.cut(f.trust_score, [-1, 59, 79, 100], labels=["LOW", "MEDIUM", "HIGH"]).astype(str)
    f["trust_detail"] = [
        f"rate check {a}/30, contracts agree {b}/15, fits contract formula {c}/20, PPO evidence {d}/20, address {e}/15"
        for a, b, c, d, e in zip(*(pts[k].astype(int) for k in ("check", "contracts", "formula", "ppo", "address")))]
    out = f.rename(columns={"npi": "NPI", "tin": "TIN", "address": "Address", "billing_code": "Billing Code",
                            "specialty": "Specialty", "rate": "Rate"})
    cols = ["NPI", "TIN", "Address", "Billing Code", "Specialty", "Rate", "trust_score", "trust_band",
            "provider_name", "npi_type", "item_name", "billing_code_type", "rate_component", "revenue_code",
            "place_of_service", "modifier", "rate_source", "n_contracts",
            "n_rate_variants", "variant_rate_min", "variant_rate_max", "tier", "address_expected_accuracy", "city", "state", "zip", "county_fips5",
            "lat", "lon", "spec_cd", "specialty_source", "provider_taxonomy", "code_in_specialty_list", "network_hint", "in_capitation_code",
            "ppo_network_evidence", "ppo_evidence_detail", "medicare_rate", "pct_of_medicare", "medicare_source",
            "rate_check", "contract_multiplier_pct", "fits_contract_formula", "contracts_agree", "trust_detail",
            "sources", "evidence"]
    out = out[cols].rename(columns={"tier": "address_tier"}).sort_values(["NPI", "TIN", "Billing Code", "Address"])
    key = ["NPI", "TIN", "Address", "Billing Code"]
    assert not out.fillna({"NPI": "", "Address": ""}).duplicated(key).any(), "duplicate NPI x TIN x Address x Code"
    # D6.5 PPO network only: providers with evidence of an HMO-only contract (HMO_SIGNAL: paid out-of-network by the
    # PPO plans, capitation contract, or IPA / Ambetter-HMO group label; logic §4.1) leave the Rate File and are kept,
    # with the reason, in their own sheet / file. MIXED (proven PPO sign + an HMO sign) stays, flagged.
    hmo = out.ppo_network_evidence.eq("HMO_SIGNAL")
    excluded = out[hmo].assign(exclusion_reason="HMO_SIGNAL: " + out.loc[hmo, "ppo_evidence_detail"].fillna(""))
    out = out[~hmo].reset_index(drop=True)
    out.to_parquet(s6 / "provider_rate_file.parquet", index=False)
    save_csv(out, s6 / "provider_rate_file.csv")
    excluded.to_parquet(s6 / "provider_rate_file_excluded_hmo.parquet", index=False)
    save_csv(excluded, s6 / "provider_rate_file_excluded_hmo.csv")
    kept = set(zip(out.NPI.fillna(""), out.TIN))
    rates = rates[[k in kept for k in zip(rates.npi.fillna(""), rates.tin)]]      # variants of PPO rows only

    # every number quoted in the Read Me comes from this run's outputs
    _f = pd.read_parquet(cfg["paths"]["stage1"] / "files.parquet")
    src_name = ", ".join(_f[_f.in_scope & _f.file_role.eq("in_network")].file_name)
    yr = (rates.medicare_source.dropna().str.extract(r"PFS (\d{4})")[0].dropna().iloc[0]
          if "medicare_source" in rates and rates.medicare_source.str.contains("PFS").any() else "")
    vp = s4 / "contract_terms_validation.parquet"
    val = pd.read_parquet(vp).set_index(["source", "cls"]).within_5pct if vp.exists() else pd.Series(dtype=float)
    v = lambda k: f"{val[k]:.1%}" if k in val.index else "n/a"  # noqa: E731
    acc = addr[addr.is_best].groupby("tier").expected_accuracy.first() if "expected_accuracy" in addr else pd.Series(dtype=float)
    a_ = lambda t: f"{acc[t]:.3f}" if t in acc.index and pd.notna(acc[t]) else "n/a"  # noqa: E731
    readme = pd.DataFrame([
        ("Rate File", "One row per NPI x TIN x Address x Billing Code. Required columns first: NPI, TIN, Address, "
                      "Billing Code, Specialty, Rate."),
        ("Rate", "Headline negotiated rate (USD). C4 keeps one rate per service: the price in the provider's "
                 "primary contract (the provider_reference that prices the most services) when several contracts "
                 "price it. Here: fee-for-service before capitation; the provider's own component (professional or "
                 "facility); no modifier; office place of service (11), else the most common one; median if several "
                 f"remain. Source file: {src_name}."),
        ("rate_component", "professional = physician / professional fee (CMS-1500). facility = institutional fee for "
                           "the same CPT/HCPCS code (UB-04), decoded from a revenue-code item whose price names the "
                           "code in additional_information; revenue_code shows the RC it was billed under."),
        ("pct_of_medicare", f"Rate / Medicare {yr} rate x 100. Professional: Physician Fee Schedule for the provider's own "
                            "California locality (RVUs x GPCIs x conversion factor; facility / non-facility by place of "
                            "service). Facility: OPPS national rate, not wage-adjusted; the file's facility amounts behave "
                            "like encounter-level prices, so read them as a relative scale. Blank: no Medicare price "
                            "(drugs, lab fee schedule, packaged codes)."),
        ("rate_check", "OK, or HIGH_VS_PEERS / LOW_VS_PEERS (robust median/MAD z-score > 3.5 among rates for the same "
                       "code, component and modifier), ABOVE_10X_MEDICARE / BELOW_30PCT_MEDICARE (professional only). "
                       "Flagged rates are real file values and are kept; filter them out for benchmarking."),
        ("trust_score", "0-100, how far to trust the row's rate: rate_check OK (30) + all contracts agree (15) + fits the "
                        "provider's recovered contract formula, X% of Medicare (20; 10 if no formula) + PPO-network "
                        "evidence (CONFIRMED 20, PRESUMED 12, MIXED 8, HMO_SIGNAL 0) + address tier (VERIFIED 15, "
                        "PROBABLE 10, UNVERIFIED 4). trust_band HIGH >= 80, MEDIUM 60-79, LOW < 60; trust_detail "
                        "shows the points."),
        ("contract_multiplier_pct", "The provider's contract recovered from its rates: 'pays X% of Medicare' (EXACT or "
                                    "LIKELY formulas only; src.stage4_contract_terms)."),
        ("Estimated Rates", "Separate sheet, NOT real file rates: HiLabs codes of the provider's specialty that the file "
                            "does not price for it, estimated as contract multiplier x Medicare. Only for EXACT formulas, "
                            f"which passed a hide-and-check test (own formula {v(('own', 'EXACT'))}, TIN formula "
                            f"{v(('TIN', 'EXACT'))} within 5% of the true rate; data/stage4/c4_contract_terms.txt)."),
        ("rate_source", "single contract, or 'primary of N contracts' when N provider_references priced the service; "
                        "all of them stay in Rate Variants (rate_median_all, rate_min, rate_max)."),
        ("Rate Variants", "Every rate row from C4 (modifiers such as 95 telehealth / 26 professional component, "
                          "other places of service), with median/min/max/count per variant."),
        ("address_tier", "The address must exist (Census geocoder, or a government record outside NPPES lists the same "
                         "building). VERIFIED: 2 independent source families agree (NPPES, Medicare enrolment/DAC, CMS "
                         "facility registries, CDPH state licence), or 1 family plus another NPI of the same TIN at the "
                         "address / DAC group-name match. PROBABLE: one non-NPPES source. UNVERIFIED: NPPES only. "
                         "NONE: no address found."),
        ("address_expected_accuracy", "Share of this tier's addresses that the California Department of Public Health "
                                      "licensing record confirms (same building or within 250 m), measured on a mapping "
                                      f"built WITHOUT that record (hold-out audit, logic D5.9): VERIFIED {a_('VERIFIED')}, "
                                      f"UNVERIFIED {a_('UNVERIFIED')}. Blank for PROBABLE (no state-listed pair to measure)."),
        ("specialty_source", "How Specialty was found, strongest first: NPPES_PRIMARY / NPPES_SECONDARY (taxonomy in the HiLabs "
                             "crosswalk), FACILITY_MAP (NUCC-verified facility taxonomy), CODE_PROFILE (the provider's priced "
                             "HiLabs codes; organizations use their whole service mix), CODE_DEFAULT (provider has no HiLabs "
                             "specialty, e.g. a hospital: the HiLabs specialty that owns this row's billing code). "
                             "provider_taxonomy keeps the provider's own NPPES taxonomy."),
        ("Specialty", "HiLabs specialty via NPPES taxonomy -> HiLabs Taxonomy-Spec Xwalk; facility taxonomies via a "
                      "NUCC-verified map; else inferred from priced codes; else 'Unmapped (<taxonomy>)'. "
                      "specialty_source tells which."),
        ("network_hint", "Business-name label of the provider group in the rate file (e.g. PPO_COUNTY, AMBETTER_HMO, "
                         "IPA). HMO-style hints are kept and flagged (logic D2.7)."),
        ("Excluded - HMO signals", "Providers removed from the Rate File because the evidence says their contract is "
                                   "HMO-only (HMO_SIGNAL): paid out-of-network by the PPO plans' own allowed-amounts file, "
                                   "a capitation contract, or an IPA / Ambetter-HMO group label. Kept with exclusion_reason."),
        ("ppo_network_evidence", "PPO_CONFIRMED: the provider practises in a CA county with no Health Net HMO network, "
                                 "or its group is labelled PPO. HMO_SIGNAL: capitation contract, IPA / Ambetter-HMO "
                                 "label, or paid out-of-network by the PPO plans (allowed-amounts file). MIXED: both. PPO_PRESUMED: direct fee-for-service, no further evidence "
                                 "(logic §4.1). Filter to PPO_CONFIRMED for a strictly proven PPO-network view."),
        ("Logic", "docs/C1_C2_C4_logic_PPO_network_and_rates.md, docs/C5_logic_TIN_NPI_address_and_specialty.md; metrics: docs/C3_file_quality_metrics.md"),
    ], columns=["Item", "Meaning"])
    # Summary sheet: what the file is and how far to trust it, readable in 30 seconds
    files = pd.read_parquet(cfg["paths"]["stage1"] / "files.parquet")
    src_file = ", ".join(files[files.in_scope & files.file_role.eq("in_network")].file_name)
    lab = pd.read_parquet(cfg["paths"]["stage2"] / "plan_labels.parquet")
    n_ppo = int(lab.is_ppo_network.sum()) if "is_ppo_network" in lab else int(lab.is_ppo.sum())
    pct = lambda m: f"{int(m.sum()):,} ({100 * m.mean():.1f}%)"  # noqa: E731
    strict = out.trust_band.eq("HIGH") & out.ppo_network_evidence.isin(["PPO_CONFIRMED", "PPO_PRESUMED"])
    vc = lambda col: "; ".join(f"{k} {v:,}" for k, v in out[col].value_counts().items())  # noqa: E731
    summary = pd.DataFrame([
        ("Source rate file", src_file),
        ("Network", f"Health Net California PPO network: {n_ppo} PPO plans (C2) use this file"),
        ("Rows (one per NPI x TIN x Address x Billing Code)", f"{len(out):,}"),
        ("Providers / groups / addresses / billing codes",
         f"{out.NPI.nunique():,} NPIs / {out.TIN.nunique():,} TINs / {out.Address.nunique():,} addresses / "
         f"{out['Billing Code'].nunique()} HiLabs codes"),
        ("Rate component", vc("rate_component") + "  (facility = hospital/ASC fee, never compared with professional)"),
        ("Address tier (rows)", vc("address_tier") + "  | measured accuracy: VERIFIED "
         f"{out[out.address_tier.eq('VERIFIED')].address_expected_accuracy.max():.1%}, UNVERIFIED "
         f"{out[out.address_tier.eq('UNVERIFIED')].address_expected_accuracy.max():.1%}"),
        ("Specialty source (rows)", vc("specialty_source")),
        ("PPO-network evidence (rows)", vc("ppo_network_evidence")),
        ("Rate check (rows)", f"OK {pct(out.rate_check.eq('OK'))}; flagged rows are real file values, kept"),
        ("Trust band (rows)", vc("trust_band")),
        ("Rate vs Medicare (professional, median)",
         f"{out[out.rate_component.eq('professional')].pct_of_medicare.median():.0f}% of Medicare 2026 for the provider's CA locality"),
        ("Strict view", f"trust_band = HIGH and ppo_network_evidence in (PPO_CONFIRMED, PPO_PRESUMED): {pct(strict)} rows"),
        ("Excluded - HMO signals sheet", f"{len(excluded):,} rows of {excluded[['NPI', 'TIN']].drop_duplicates().shape[0]} "
                                         "providers moved OUT of the Rate File: evidence of an HMO-only contract (paid "
                                         "out-of-network by the PPO plans, capitation, or IPA / Ambetter-HMO label). Kept "
                                         "for transparency with exclusion_reason; not PPO-network rates."),
        ("Estimated Rates sheet", "separate, NOT file rates: contract multiplier x Medicare, only for formulas that passed "
                                  "a hide-and-check test"),
        ("How to use", "Filter the Rate File sheet: Billing Code + Specialty + address_tier / trust_band. Rate Variants "
                       "holds every price behind a headline rate; Read Me defines every column."),
    ], columns=["Item", "Value"])
    ep = s4 / "estimated_rates.parquet"
    est = pd.read_parquet(ep) if ep.exists() else pd.DataFrame()
    if len(est):
        est = est[[k in kept for k in zip(est.npi.fillna(""), est.tin)]]
    xp = s6 / "provider_rate_file.xlsx"
    try:
        if xp.exists():
            open(xp, "r+b").close()          # Excel locks an open workbook on Windows
    except PermissionError:
        print(f"WARNING: {xp} is open in Excel; close it and re-run (parquet and CSV are written)")
        return
    with pd.ExcelWriter(xp, engine="openpyxl") as xw:
        summary.to_excel(xw, sheet_name="Summary", index=False)
        out.to_excel(xw, sheet_name="Rate File", index=False)
        rates.to_excel(xw, sheet_name="Rate Variants", index=False)
        est.to_excel(xw, sheet_name="Estimated Rates", index=False)
        excluded.to_excel(xw, sheet_name="Excluded - HMO signals", index=False)
        readme.to_excel(xw, sheet_name="Read Me", index=False)
        for name, df in (("Rate File", out), ("Rate Variants", rates), ("Estimated Rates", est),
                         ("Excluded - HMO signals", excluded)):
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for i, col in enumerate(df.columns, 1):
                ws.column_dimensions[ws.cell(1, i).column_letter].width = min(45, max(10, len(str(col)) + 2))
        xw.sheets["Summary"].column_dimensions["A"].width = 48
        xw.sheets["Summary"].column_dimensions["B"].width = 150
        xw.sheets["Read Me"].column_dimensions["A"].width = 16
        xw.sheets["Read Me"].column_dimensions["B"].width = 140

    print(f"rate file rows: {len(out):,} | NPIs {out.NPI.nunique():,} | TINs {out.TIN.nunique():,} | "
          f"codes {out['Billing Code'].nunique()} | addresses {out.Address.nunique():,}")
    print("address_tier (rows):", out.address_tier.value_counts().to_dict())
    print("specialty_source (rows):", out.specialty_source.value_counts(dropna=False).to_dict())
    print(f"wrote {s6 / 'provider_rate_file.xlsx'}")


if __name__ == "__main__":
    main()
