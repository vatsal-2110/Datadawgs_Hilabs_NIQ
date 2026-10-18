"""Specialty for every NPI in the PTD file (needed for the final rate file and for C7 peers).

Rules, first match wins (logic D5.6); every row records specialty_source:
  1 NPPES_PRIMARY     NPI's primary NPPES taxonomy is in the HiLabs Taxonomy-Spec Xwalk
  2 NPPES_SECONDARY   another of its NPPES taxonomies is in the Xwalk
  3 FACILITY_MAP      a taxonomy is in config specialty.facility_taxonomy_map (NUCC-verified facility codes)
  4 CODE_PROFILE      from the HiLabs codes the NPI is priced on: score each specialty by the share of its
                      Spec-Proc weight those codes cover; accept the top one only if it beats the runner-up
                      by config specialty.code_profile_min_ratio
  5 UNMAPPED          kept, labelled with the NUCC description of its primary taxonomy

Output: data/stage5/npi_specialty.parquet (+ .csv)
Usage : python -m src.stage5_specialty
"""
from __future__ import annotations

import pandas as pd

from src.common.normalize import load_config, save_csv


def main():
    cfg = load_config()
    s5 = cfg["paths"]["stage5"]
    x = pd.read_excel(cfg["paths"]["hilabs_xlsx"], sheet_name=None, dtype=str)
    xw = dict(zip(x["Taxonomy-Spec Xwalk"]["Taxonomy Code"], x["Taxonomy-Spec Xwalk"].SPEC_CD))
    sp = x["Spec-Proc Weights"].assign(w=lambda d: d.SPEC_PROC_WEIGHT.astype(float))
    spec_name = {**dict(zip(sp.SPEC_CD, sp.SPEC_DESCRIPTION)),
                 **dict(zip(x["Specialty Weights"]["Spec Code"], x["Specialty Weights"].Specialty))}
    fac = {k: str(v) for k, v in cfg["specialty"]["facility_taxonomy_map"].items()}
    nucc = pd.read_csv(cfg["paths"]["nucc_csv"], dtype=str, encoding="latin-1")
    nucc_desc = dict(zip(nucc.Code, nucc["Display Name"].fillna(nucc.Classification)))
    assert set(fac) <= set(nucc.Code), "facility_taxonomy_map has codes not in NUCC"

    nppes = pd.read_parquet(s5 / "nppes_npi.parquet")
    rates = pd.read_parquet(cfg["paths"]["stage4"] / "rates.parquet")
    # professional codes only: a facility fee says what the building does, not the clinician's specialty
    priced = rates[rates.component.eq("professional")].dropna(subset=["npi"]).groupby("npi").billing_code.agg(set).to_dict()
    spec_tot = sp.groupby("SPEC_CD").w.sum()
    ratio = cfg["specialty"]["code_profile_min_ratio"]

    rows = []
    for r in nppes.itertuples():
        codes = (r.taxonomy_codes or "").split("|") if r.found else []
        prim = r.primary_taxonomy if r.found else None
        spec, src, note = None, "UNMAPPED", ""
        if prim in xw:
            spec, src = xw[prim], "NPPES_PRIMARY"
        elif any(c in xw for c in codes):
            spec, src = xw[next(c for c in codes if c in xw)], "NPPES_SECONDARY"
        elif any(c in fac for c in codes):
            spec, src = fac[next(c for c in codes if c in fac)], "FACILITY_MAP"
        else:
            pc = priced.get(r.npi, set())
            if pc:
                sc = (sp[sp.PROC_CD.isin(pc)].groupby("SPEC_CD").w.sum() / spec_tot).dropna().sort_values(ascending=False)
                if len(sc) and (len(sc) == 1 or sc.iloc[0] >= ratio * sc.iloc[1]):
                    spec, src = sc.index[0], "CODE_PROFILE"
                    note = f"top {sc.index[0]}={sc.iloc[0]:.3f}" + (f" vs {sc.index[1]}={sc.iloc[1]:.3f}" if len(sc) > 1 else "")
                elif len(sc):
                    note = f"ambiguous: {sc.index[0]}={sc.iloc[0]:.3f} vs {sc.index[1]}={sc.iloc[1]:.3f}"
        rows.append({"npi": r.npi, "enumeration_type": getattr(r, "enumeration_type", None),
                     "primary_taxonomy": prim, "primary_taxonomy_desc": nucc_desc.get(prim) if prim else None,
                     "spec_cd": spec, "specialty": spec_name.get(spec) if spec else
                     (f"Unmapped ({nucc_desc.get(prim, 'no taxonomy')})" if prim else "Unmapped (NPI not in NPPES)"),
                     "specialty_source": src, "specialty_note": note})
    out = pd.DataFrame(rows)
    out.to_parquet(s5 / "npi_specialty.parquet", index=False)
    save_csv(out, s5 / "npi_specialty.csv")
    print(pd.crosstab(out.specialty_source, out.enumeration_type, margins=True).to_string())
    print("\ntop unmapped:", out[out.specialty_source == "UNMAPPED"].specialty.value_counts().head(10).to_dict())


if __name__ == "__main__":
    main()
