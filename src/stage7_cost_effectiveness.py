"""C7 (bonus): is a provider cost-effective compared with similar providers?

"Similar" (peer group) = same billing code AND same rate component (professional fee vs facility fee: a hospital's
facility charge is never compared with a physician's fee) AND same HiLabs specialty AND same county (best address);
if that group has fewer than MIN_PEERS distinct providers, widen to the same code + specialty statewide
(peer_scope records which). Rates are the headline rates of the C6 file (standard, no-modifier service).

Index per provider-code (index_basis says which):
  same_code                  provider rate / peer median rate on the SAME code (peer group above, >= MIN_PEERS)
  pct_of_medicare            when the code has too few peers (professional rows): provider's rate as % of its own
                             locality's Medicare / median % of Medicare of its specialty peers (county, else
                             statewide, else all professionals), pooled over codes. Valid because Health Net's
                             professional schedule is Medicare-pegged: a provider's % of Medicare varies about half
                             as much across its own codes (median log-sd 0.27) as across providers (0.55), so one code
                             already reveals most of the contract level.
Metrics per provider (NPI x TIN):
  rate_index (per code)      the index above                              (1.00 = typical, 0.80 = 20% cheaper)
  weighted_cost_index        sum(w * rate_index) / sum(w); w = HiLabs SPEC_PROC_WEIGHT of the code for the
                             provider's specialty (codes that matter most to that specialty count most)
  pct_codes_below_median     share of the provider's codes priced below the peer median (consistency)
  n_codes_compared           codes with a valid peer group
Label (label_confidence HIGH >= 3 codes, MEDIUM 2, LOW 1): INSUFFICIENT_DATA if no code can be compared; COST_EFFECTIVE if weighted_cost_index <= 0.90 and
       pct_codes_below_median >= 0.60; HIGH_COST if weighted_cost_index >= 1.10 and pct_codes_below_median <= 0.40;
       else AVERAGE.
Also reported: median_pct_of_medicare = the provider's typical professional rate as % of Medicare for its own
California locality (absolute contract level, comparable across codes). Rates flagged by C4 rate_check (robust
outliers, >10x Medicare) are excluded from every peer comparison.
Providers with no HiLabs specialty (specialty_source CODE_DEFAULT, mostly hospitals) are compared with each other:
peer specialty = "ALL" (same code + same component), weight = the code's highest HiLabs weight.

QUALITY (C7 v3): cost-effective should mean a low price at acceptable quality. Two CMS quality sources, joined exactly:
  clinicians  CMS Quality Payment Program, PY 2024 Clinician Public Reporting: Overall MIPS Performance (Provider Data
              Catalog dataset a174-a962), by NPI: quality_category_score (0-100), else final_mips_score
  hospitals   CMS Hospital General Information (xubh-q36u): hospital_overall_rating (1-5 stars), by CCN; CCN -> NPI from
              the CDPH Licensed and Certified Healthcare Facility Listing
  quality_good = MIPS score >= config c7.mips_good (75 = CMS PY2024 performance threshold) or stars >= c7.stars_good (3 =
                 national midpoint)
  value_label  HIGH_VALUE (price at/below peers, quality good) | LOW_PRICE_LOW_QUALITY | HIGH_PRICE_HIGH_QUALITY |
               LOW_VALUE (price above peers, quality not good) | PRICE_ONLY (no CMS quality data: label = price only)
  label_with_quality  the price label, except COST_EFFECTIVE with poor quality -> CHEAP_BUT_LOW_QUALITY
Price only for the rest: no quality or utilization data is available in PTD, so this measures negotiated price levels.

Output: data/stage7/provider_cost_effectiveness.parquet (+ .csv), data/stage7/code_rate_index.parquet,
        data/stage7/C7_cost_effectiveness.xlsx (Summary, Providers, Best value by specialty, Code comparisons, Read Me)
Usage : python -m src.stage7_cost_effectiveness
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import hashlib
import json

from src.common.normalize import load_config, save_csv

MIN_PEERS, MIN_CODES = 5, 1


def quality(cfg, npis: list) -> pd.DataFrame:
    """CMS quality per NPI: MIPS (clinicians) and hospital overall star rating (hospitals, CCN via CDPH)."""
    from src.stage5_address_map import load_cdph
    from src.stage5_fetch_sources import post_json
    cache = cfg["paths"]["cache"] / "quality"
    cache.mkdir(parents=True, exist_ok=True)
    url = cfg["sources"]["provider_data_query"].format(id=cfg["c7"]["mips_dataset"])
    rows = []
    for i in range(0, len(npis), 300):
        chunk = npis[i:i + 300]
        cf = cache / f"mips_{hashlib.sha1(','.join(chunk).encode()).hexdigest()[:16]}.json"
        if not cf.exists():
            j = post_json(url, {"conditions": [{"property": "npi", "value": chunk, "operator": "in"}], "limit": 1500}, 120)
            cf.write_text(json.dumps(j.get("results") or []), encoding="utf-8")
        rows += json.loads(cf.read_text(encoding="utf-8"))
    out = []
    if rows:
        m = pd.DataFrame(rows)
        num = lambda c: pd.to_numeric(m[c].replace("", None), errors="coerce")  # noqa: E731
        m["qs"], m["fs"] = num("quality_category_score"), num("final_mips_score")
        m["score"] = m.qs.fillna(m.fs)
        m = m.dropna(subset=["score"]).sort_values("source").drop_duplicates("npi")  # 'apm' < 'group' < 'individual'
        for r in m.itertuples():
            out.append((r.npi, "MIPS", float(r.score),
                        f"CMS MIPS PY2024 ({r.source}): quality {r.quality_category_score or 'n/a'}, final {r.final_mips_score}"))
    h = cfg["paths"]["cache"] / "registries" / f"xubh-q36u_{cfg['scope']['state']}.json"
    if h.exists():
        hd = pd.DataFrame(json.loads(h.read_text(encoding="utf-8")))
        hd = hd[pd.to_numeric(hd.hospital_overall_rating, errors="coerce").notna()]
        cdph = load_cdph(cfg)
        raw = pd.read_csv(cfg["paths"]["cdph"] / "health_facility_locations.csv", dtype=str, encoding="latin-1")
        raw["npi"] = raw.NPI.str.extract(r"(\d{10})")[0]
        link = raw.dropna(subset=["npi", "CCN"])[["npi", "CCN"]].drop_duplicates()
        x = link[link.npi.isin(npis)].merge(hd[["facility_id", "facility_name", "hospital_overall_rating"]],
                                            left_on="CCN", right_on="facility_id")
        have = {o[0] for o in out}
        for r in x.drop_duplicates("npi").itertuples():
            if r.npi not in have:
                out.append((r.npi, "HOSPITAL_STARS", float(r.hospital_overall_rating),
                            f"CMS hospital overall rating {r.hospital_overall_rating}/5 ({r.facility_name}, CCN {r.CCN})"))
        del cdph
    return pd.DataFrame(out, columns=["NPI", "quality_basis", "quality_score", "quality_detail"])


def write_excel(cfg, prov, base, s7):
    xp = s7 / "C7_cost_effectiveness.xlsx"
    try:
        if xp.exists():
            open(xp, "r+b").close()
    except PermissionError:
        print(f"WARNING: {xp} is open in Excel; close it and re-run")
        return
    med = prov.groupby("label").median_pct_of_medicare.median().round(1)
    vc = lambda c: "; ".join(f"{k} {v:,}" for k, v in prov[c].value_counts().items())  # noqa: E731
    hs = prov[prov.quality_basis.eq("HOSPITAL_STARS")]
    hosp = hs.groupby(hs.weighted_cost_index > 1).quality_score.mean().round(2).to_dict()
    summ = pd.DataFrame([
        ("Question", "Is a provider cost-effective compared with similar providers? (PS C7, bonus)"),
        ("Input", "C6 Rate File: PPO network only (HMO-signal providers excluded), flagged rates (rate_check) excluded"),
        ("Similar providers", "same billing code + same component (professional / facility) + same HiLabs specialty + same "
                              "county (else statewide); providers without a HiLabs specialty compared with each other"),
        ("Providers scored", f"{len(prov):,}"),
        ("Price label", vc("label")),
        ("Label confidence (codes behind it)", vc("label_confidence")),
        ("Check against Medicare (median % of Medicare by label)", "; ".join(f"{k} {v}%" for k, v in med.items())),
        ("Quality data (CMS)", vc("quality_basis") + "  (MIPS = clinicians, PY2024; HOSPITAL_STARS = CMS overall rating)"),
        ("Value label", vc("value_label")),
        ("Hospitals: price vs quality", f"mean stars, price at/below peers {hosp.get(False, 'n/a')} vs above peers "
                                        f"{hosp.get(True, 'n/a')}"),
        ("Limits", "Price is negotiated price, not total spend (no utilization data in PTD). Quality exists only for "
                   "providers CMS rates; the rest are PRICE_ONLY."),
    ], columns=["Item", "Value"])
    best = (prov[prov.label.eq("COST_EFFECTIVE") & prov.label_confidence.eq("HIGH") & prov.spec_cd.ne("ALL")]
            .sort_values(["specialty", "weighted_cost_index"]).groupby("specialty").head(5))
    readme = pd.DataFrame([
        ("weighted_cost_index", "sum(HiLabs weight x rate index) / sum(weight); rate index = provider rate / peer median "
                                "(1.00 typical, 0.80 = 20% cheaper)"),
        ("label", "COST_EFFECTIVE: index <= 0.90 and >= 60% of codes below peers; HIGH_COST: index >= 1.10 and <= 40% "
                  "below; else AVERAGE"),
        ("label_confidence", "HIGH >= 3 codes compared, MEDIUM 2, LOW 1"),
        ("median_pct_of_medicare", "provider's typical professional rate / Medicare for its CA locality"),
        ("quality_basis / quality_score", "MIPS: CMS PY2024 MIPS quality category score (0-100, else final score), "
                                          "Provider Data Catalog a174-a962. HOSPITAL_STARS: CMS hospital overall rating 1-5 "
                                          "(xubh-q36u), linked by CCN through the CDPH facility listing. NONE: not rated"),
        ("value_label", "price at/below peers x quality good (MIPS >= c7.mips_good, stars >= c7.stars_good in config)"),
        ("label_with_quality", "price label, except COST_EFFECTIVE with poor CMS quality -> CHEAP_BUT_LOW_QUALITY"),
        ("Logic", "docs/logic_rate_file_c6_c7.md, section C7"),
    ], columns=["Item", "Meaning"])
    with pd.ExcelWriter(xp, engine="openpyxl") as xw:
        for name, df in (("Summary", summ), ("Providers", prov), ("Best value by specialty", best),
                         ("Code comparisons", base), ("Read Me", readme)):
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            for i, col in enumerate(df.columns, 1):
                ws.column_dimensions[ws.cell(1, i).column_letter].width = min(60, max(10, len(str(col)) + 2))
        xw.sheets["Summary"].column_dimensions["B"].width = 140
        xw.sheets["Read Me"].column_dimensions["B"].width = 140
    print(f"wrote {xp}")


def main():
    cfg = load_config()
    s6, s7 = cfg["paths"]["stage6"], cfg["paths"]["stage7"]
    s7.mkdir(parents=True, exist_ok=True)
    f = pd.read_parquet(s6 / "provider_rate_file.parquet")
    sp = pd.read_excel(cfg["paths"]["hilabs_xlsx"], sheet_name="Spec-Proc Weights", dtype=str)
    w = dict(zip(zip(sp.SPEC_CD, sp.PROC_CD), sp.SPEC_PROC_WEIGHT.astype(float)))

    # one rate per provider x code (addresses only repeat the same rate); county from the first address
    # implausible rates (C4 rate_check: robust outliers / >10x Medicare) never set or join a peer median
    n_flag = f[f.rate_check.ne("OK")][["NPI", "TIN", "Billing Code"]].drop_duplicates().shape[0]
    # CODE_DEFAULT specialties describe the code, not the provider: those providers (mostly hospitals) are compared
    # with each other in a specialty-free peer group "ALL" (same code + same component)
    f = f.copy()
    cd = f.specialty_source.eq("CODE_DEFAULT")
    f.loc[cd, "spec_cd"], f.loc[cd, "Specialty"] = "ALL", "No HiLabs specialty (compared with all such providers)"
    base = (f[f.rate_check.eq("OK")].dropna(subset=["NPI", "spec_cd"]).sort_values("address_tier")
            .groupby(["NPI", "TIN", "Billing Code", "rate_component"]).agg(rate=("Rate", "first"), spec_cd=("spec_cd", "first"),
                                                         specialty=("Specialty", "first"),
                                                         county=("county_fips5", "first"),
                                                         pct_mc=("pct_of_medicare", "first")).reset_index())
    base["county"] = base.county.fillna("UNKNOWN")
    cnt = base.groupby(["Billing Code", "rate_component", "spec_cd", "county"]).NPI.transform("nunique")
    med_cty = base.groupby(["Billing Code", "rate_component", "spec_cd", "county"]).rate.transform("median")
    cnt_st = base.groupby(["Billing Code", "rate_component", "spec_cd"]).NPI.transform("nunique")
    med_st = base.groupby(["Billing Code", "rate_component", "spec_cd"]).rate.transform("median")
    use_cty = (cnt >= MIN_PEERS) & base.county.ne("UNKNOWN")
    base["peer_scope"] = use_cty.map({True: "county", False: "statewide"})
    base["peer_n"] = cnt.where(use_cty, cnt_st)
    base["peer_median"] = med_cty.where(use_cty, med_st)
    code_ok = base.peer_n >= MIN_PEERS
    # Medicare-normalised fallback: pooled over codes within specialty (+ county)
    pr = base.rate_component.eq("professional") & base.pct_mc.notna()
    m = base[pr]
    n_c = m.groupby(["spec_cd", "county"]).NPI.transform("nunique")
    n_s = m.groupby("spec_cd").NPI.transform("nunique")
    mc_peer = m.groupby(["spec_cd", "county"]).pct_mc.transform("median").where(
        (n_c >= MIN_PEERS) & m.county.ne("UNKNOWN"),
        m.groupby("spec_cd").pct_mc.transform("median").where(n_s >= MIN_PEERS, m.pct_mc.median()))
    base["mc_peer_median_pct"] = mc_peer
    base["rate_index"] = (base.rate / base.peer_median).where(code_ok, base.pct_mc / base.mc_peer_median_pct)
    base["index_basis"] = pd.Series("same_code", index=base.index).where(code_ok, "pct_of_medicare")
    base.loc[~code_ok, "peer_scope"] = "specialty, Medicare-normalised"
    base = base[base.rate_index.notna()].copy()
    wmax = sp.assign(x=sp.SPEC_PROC_WEIGHT.astype(float)).groupby("PROC_CD").x.max().to_dict()
    base["w"] = [wmax.get(c, 0.0) if s == "ALL" else w.get((s, c), 0.0) for s, c in zip(base.spec_cd, base["Billing Code"])]
    base["w"] = base.w.where(base.w > 0, base.w[base.w > 0].min() if (base.w > 0).any() else 1.0)  # unlisted code: smallest weight
    base.to_parquet(s7 / "code_rate_index.parquet", index=False)

    g = base.groupby(["NPI", "TIN"])
    prov = g.apply(lambda d: pd.Series({
        "specialty": d.specialty.iloc[0], "spec_cd": d.spec_cd.iloc[0], "county": d.county.iloc[0],
        "n_codes_compared": len(d),
        "weighted_cost_index": (d.w * d.rate_index).sum() / d.w.sum(),
        "pct_codes_below_median": (d.rate_index < 1).mean(),
        "share_same_code_index": (d.index_basis == "same_code").mean(),
        "share_county_peers": (d.peer_scope == "county").mean(),
        # absolute, payer-independent level: median rate as % of the provider's own-locality Medicare (professional)
        "median_pct_of_medicare": (lambda x: x.median() if len(x) else None)(
            d[d.rate_component.eq("professional")].pct_mc.dropna())}),
        include_groups=False).reset_index()
    prov = prov.round({"median_pct_of_medicare": 1, "weighted_cost_index": 3, "pct_codes_below_median": 3,
                       "share_county_peers": 3, "share_same_code_index": 3})
    prov["label_confidence"] = pd.cut(prov.n_codes_compared, [0, 1, 2, 10 ** 6], labels=["LOW", "MEDIUM", "HIGH"]).astype(str)
    prov["label"] = "AVERAGE"
    prov.loc[(prov.weighted_cost_index <= 0.90) & (prov.pct_codes_below_median >= 0.60), "label"] = "COST_EFFECTIVE"
    prov.loc[(prov.weighted_cost_index >= 1.10) & (prov.pct_codes_below_median <= 0.40), "label"] = "HIGH_COST"
    prov.loc[prov.n_codes_compared < MIN_CODES, "label"] = "INSUFFICIENT_DATA"
    # ---- quality (CMS), value label
    q = quality(cfg, sorted(prov.NPI.dropna().unique()))
    prov = prov.merge(q, on="NPI", how="left")
    prov["quality_basis"] = prov.quality_basis.fillna("NONE")
    c7 = cfg["c7"]
    good = np.where(prov.quality_basis.eq("MIPS"), prov.quality_score >= c7["mips_good"],
                    np.where(prov.quality_basis.eq("HOSPITAL_STARS"), prov.quality_score >= c7["stars_good"], np.nan))
    prov["quality_good"] = pd.Series(good, index=prov.index).map({1.0: True, 0.0: False})
    low = prov.weighted_cost_index <= 1.0
    prov["value_label"] = "PRICE_ONLY"
    has_q = prov.quality_good.notna()
    qg = prov.quality_good.astype("boolean").fillna(False).astype(bool)
    prov.loc[has_q & low & qg, "value_label"] = "HIGH_VALUE"
    prov.loc[has_q & low & ~qg, "value_label"] = "LOW_PRICE_LOW_QUALITY"
    prov.loc[has_q & ~low & qg, "value_label"] = "HIGH_PRICE_HIGH_QUALITY"
    prov.loc[has_q & ~low & ~qg, "value_label"] = "LOW_VALUE"
    prov["label_with_quality"] = prov.label.where(~(prov.label.eq("COST_EFFECTIVE") & has_q & ~qg), "CHEAP_BUT_LOW_QUALITY")
    nm = pd.read_parquet(cfg["paths"]["stage5"] / "nppes_npi.parquet")[["npi", "name"]].rename(
        columns={"npi": "NPI", "name": "provider_name"})
    prov = prov.merge(nm, on="NPI", how="left")
    lead = ["NPI", "TIN", "provider_name", "specialty", "county", "label", "label_confidence", "label_with_quality",
            "value_label", "weighted_cost_index", "median_pct_of_medicare", "quality_basis", "quality_score",
            "quality_detail"]
    prov = prov[lead + [c for c in prov.columns if c not in lead]]
    prov.to_parquet(s7 / "provider_cost_effectiveness.parquet", index=False)
    save_csv(prov, s7 / "provider_cost_effectiveness.csv")
    write_excel(cfg, prov, base, s7)
    print(f"providers scored: {len(prov):,} | code comparisons: {len(base):,} | peer scope: "
          f"{base.peer_scope.value_counts().to_dict()}")
    print(f"rates excluded as implausible (rate_check): {n_flag:,} provider-code combinations")
    print("median % of Medicare (professional) by label:",
          prov.groupby("label").median_pct_of_medicare.median().round(1).to_dict())
    print("labels:", prov.label.value_counts().to_dict())
    print("quality basis:", prov.quality_basis.value_counts().to_dict(), "| value labels:", prov.value_label.value_counts().to_dict())
    print("labels x confidence:", pd.crosstab(prov.label, prov.label_confidence).to_dict())
    print("index basis (provider-codes):", base.index_basis.value_counts().to_dict())
    print("weighted_cost_index quantiles:", prov[prov.label != 'INSUFFICIENT_DATA'].weighted_cost_index
          .quantile([.1, .25, .5, .75, .9]).round(3).to_dict())


if __name__ == "__main__":
    main()
