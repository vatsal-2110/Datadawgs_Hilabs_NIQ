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
Price only: no quality or utilization data is available in PTD, so this measures negotiated price levels.

Output: data/stage7/provider_cost_effectiveness.parquet (+ .csv), data/stage7/code_rate_index.parquet
Usage : python -m src.stage7_cost_effectiveness
"""
from __future__ import annotations

import pandas as pd

from src.common.normalize import load_config

MIN_PEERS, MIN_CODES = 5, 1


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
    base["w"] = [w.get((s, c), 0.0) for s, c in zip(base.spec_cd, base["Billing Code"])]
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
    prov.to_parquet(s7 / "provider_cost_effectiveness.parquet", index=False)
    prov.to_csv(s7 / "provider_cost_effectiveness.csv", index=False, encoding="utf-8")
    print(f"providers scored: {len(prov):,} | code comparisons: {len(base):,} | peer scope: "
          f"{base.peer_scope.value_counts().to_dict()}")
    print(f"rates excluded as implausible (rate_check): {n_flag:,} provider-code combinations")
    print("median % of Medicare (professional) by label:",
          prov.groupby("label").median_pct_of_medicare.median().round(1).to_dict())
    print("labels:", prov.label.value_counts().to_dict())
    print("labels x confidence:", pd.crosstab(prov.label, prov.label_confidence).to_dict())
    print("index basis (provider-codes):", base.index_basis.value_counts().to_dict())
    print("weighted_cost_index quantiles:", prov[prov.label != 'INSUFFICIENT_DATA'].weighted_cost_index
          .quantile([.1, .25, .5, .75, .9]).round(3).to_dict())


if __name__ == "__main__":
    main()
