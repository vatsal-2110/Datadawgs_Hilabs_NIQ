"""C6: build the provider rate file at NPI x TIN x address x billing code level (deliverable 1).

Inputs : data/stage4/rates.parquet, data/stage5/npi_tin_address.parquet, data/stage5/npi_specialty.parquet,
         data/stage5/nppes_npi.parquet
Outputs: data/stage6/provider_rate_file.xlsx  sheets: "Rate File", "Rate Variants", "Read Me"
         data/stage6/provider_rate_file.parquet (+ .csv)

Headline rate per NPI x TIN x code (logic D6.1):
  1. prefer rows with NO modifier (standard/global service; 95/GT telehealth, 26/TC components are variants)
  2. among them prefer place of service containing 11 (office), else the most common place of service
  3. if several remain, median of their rates
All variants stay in the "Rate Variants" sheet.

Addresses per NPI x TIN (logic D6.2): every VERIFIED and PROBABLE address; if the pair has none, its single
best address (UNVERIFIED / NONE) so no rate is dropped. The tier is a column.

Usage: python -m src.stage6_build_rate_file
"""
from __future__ import annotations

import pandas as pd

from src.common.normalize import load_config


def headline(rates: pd.DataFrame) -> pd.DataFrame:
    r = rates.copy()
    r["no_mod"] = r.modifier.eq("")
    r["office"] = r.service_code.str.split("|").map(lambda s: "11" in s)
    k = ["npi", "tin", "billing_code"]
    r["_npi"] = r.npi.fillna("")
    kk = ["_npi", "tin", "billing_code"]
    pos_mode = r.groupby(kk + ["service_code"]).size().reset_index(name="n").sort_values("n", ascending=False) \
        .drop_duplicates(kk).set_index(kk).service_code
    r["is_mode_pos"] = r.set_index(kk).index.map(pos_mode).values == r.service_code.values
    # preference rank: no modifier first, then office, then modal place of service
    r["pref"] = (~r.no_mod).astype(int) * 4 + (~r.office).astype(int) * 2 + (~r.is_mode_pos).astype(int)
    best_pref = r.groupby(kk).pref.transform("min")
    chosen = r[r.pref == best_pref]
    agg = chosen.groupby(kk).agg(rate=("rate", "median"), place_of_service=("service_code", lambda s: "; ".join(sorted(set(s)))),
                                 modifier=("modifier", lambda s: "; ".join(sorted(set(s))) or ""),
                                 item_name=("item_name", "first"), billing_class=("billing_class", "first"),
                                 billing_code_type=("billing_code_type", "first"), hilabs_spec_cds=("hilabs_spec_cds", "first"),
                                 network_hint=("network_hint", lambda s: "|".join(sorted({x for v in s for x in v.split("|") if x}))),
                                 in_capitation_code=("in_capitation_code", "max")).reset_index()
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
    a = a[["npi", "tin", "address", "city", "state", "zip", "county_fips5", "lat", "lon", "tier", "sources", "evidence"]]
    for d in (h, a):
        d["_npi"] = d.npi.fillna("")
    f = h.merge(a.drop(columns="npi"), on=["_npi", "tin"], how="left")
    f["_npi"] = f._npi.replace("", None)
    f = f.merge(spec[["npi", "spec_cd", "specialty", "specialty_source"]], left_on="_npi", right_on="npi",
                how="left", suffixes=("", "_s")).drop(columns=["npi_s"])
    f = f.merge(nppes.rename(columns={"npi": "_npi", "enumeration_type": "npi_type", "name": "provider_name"}),
                on="_npi", how="left")
    f["npi"] = f["_npi"]
    f["specialty"] = f.specialty.where(f.specialty.notna(), f.npi.map(
        lambda n: "Unmapped (TIN-only contract, no NPI)" if n is None or pd.isna(n) else "Unmapped (NPI not in NPPES)"))
    f["specialty_source"] = f.specialty_source.fillna("UNMAPPED")
    f["code_in_specialty_list"] = [bool(s) and isinstance(c, str) and s in c.split(",")
                                   for s, c in zip(f.spec_cd, f.hilabs_spec_cds)]
    f["tier"] = f.tier.fillna("NONE")
    out = f.rename(columns={"npi": "NPI", "tin": "TIN", "address": "Address", "billing_code": "Billing Code",
                            "specialty": "Specialty", "rate": "Rate"})
    cols = ["NPI", "TIN", "Address", "Billing Code", "Specialty", "Rate",
            "provider_name", "npi_type", "item_name", "billing_code_type", "place_of_service", "modifier",
            "n_rate_variants", "variant_rate_min", "variant_rate_max", "tier", "city", "state", "zip", "county_fips5",
            "lat", "lon", "spec_cd", "specialty_source", "code_in_specialty_list", "network_hint", "in_capitation_code",
            "sources", "evidence"]
    out = out[cols].rename(columns={"tier": "address_tier"}).sort_values(["NPI", "TIN", "Billing Code", "Address"])
    key = ["NPI", "TIN", "Address", "Billing Code"]
    assert not out.fillna({"NPI": "", "Address": ""}).duplicated(key).any(), "duplicate NPI x TIN x Address x Code"
    out.to_parquet(s6 / "provider_rate_file.parquet", index=False)
    out.to_csv(s6 / "provider_rate_file.csv", index=False, encoding="utf-8")

    readme = pd.DataFrame([
        ("Rate File", "One row per NPI x TIN x Address x Billing Code. Required columns first: NPI, TIN, Address, "
                      "Billing Code, Specialty, Rate."),
        ("Rate", "Headline negotiated rate (USD): no-modifier rate at office place of service (11) if present, else "
                 "the most common place of service; median if several remain. Source file: Health Net CA in-network "
                 "file (Centene, 2026-09-30)."),
        ("Rate Variants", "Every rate row from C4 (modifiers such as 95 telehealth / 26 professional component, "
                          "other places of service), with median/min/max/count per variant."),
        ("address_tier", "VERIFIED: Census-geocoded and confirmed by 2 independent sources (NPPES + CMS DAC) or by 1 "
                         "source plus another NPI of the same TIN at the address / DAC group-name match. PROBABLE: "
                         "geocoded, one current source. UNVERIFIED: otherwise. NONE: no address found."),
        ("Specialty", "HiLabs specialty via NPPES taxonomy -> HiLabs Taxonomy-Spec Xwalk; facility taxonomies via a "
                      "NUCC-verified map; else inferred from priced codes; else 'Unmapped (<taxonomy>)'. "
                      "specialty_source tells which."),
        ("network_hint", "Business-name label of the provider group in the rate file (e.g. PPO_COUNTY, AMBETTER_HMO, "
                         "IPA). HMO-style hints are kept and flagged (logic D2.7)."),
        ("Logic", "docs/logic_ppo_network.md, docs/logic_address_mapping.md; metrics: docs/file_quality_metrics.md"),
    ], columns=["Item", "Meaning"])
    with pd.ExcelWriter(s6 / "provider_rate_file.xlsx", engine="openpyxl") as xw:
        out.to_excel(xw, sheet_name="Rate File", index=False)
        rates.to_excel(xw, sheet_name="Rate Variants", index=False)
        readme.to_excel(xw, sheet_name="Read Me", index=False)
        for name, df in (("Rate File", out), ("Rate Variants", rates)):
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for i, col in enumerate(df.columns, 1):
                ws.column_dimensions[ws.cell(1, i).column_letter].width = min(45, max(10, len(str(col)) + 2))
        xw.sheets["Read Me"].column_dimensions["A"].width = 16
        xw.sheets["Read Me"].column_dimensions["B"].width = 140

    print(f"rate file rows: {len(out):,} | NPIs {out.NPI.nunique():,} | TINs {out.TIN.nunique():,} | "
          f"codes {out['Billing Code'].nunique()} | addresses {out.Address.nunique():,}")
    print("address_tier (rows):", out.address_tier.value_counts().to_dict())
    print("specialty_source (rows):", out.specialty_source.value_counts(dropna=False).to_dict())
    print(f"wrote {s6 / 'provider_rate_file.xlsx'}")


if __name__ == "__main__":
    main()
