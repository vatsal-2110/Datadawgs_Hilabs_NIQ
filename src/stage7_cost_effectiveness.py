"""C7 (bonus): is a provider cost-effective compared with similar providers?

"Similar" (peer group) = same billing code AND same HiLabs specialty AND same county (best address);
if that group has fewer than MIN_PEERS distinct providers, widen to the same code + specialty statewide
(peer_scope records which). Rates are the headline rates of the C6 file (standard, no-modifier service).

Metrics per provider (NPI x TIN):
  rate_index (per code)      provider rate / peer median rate            (1.00 = typical, 0.80 = 20% cheaper)
  weighted_cost_index        sum(w * rate_index) / sum(w); w = HiLabs SPEC_PROC_WEIGHT of the code for the
                             provider's specialty (codes that matter most to that specialty count most)
  pct_codes_below_median     share of the provider's codes priced below the peer median (consistency)
  n_codes_compared           codes with a valid peer group
Label: INSUFFICIENT_DATA if n_codes_compared < MIN_CODES; COST_EFFECTIVE if weighted_cost_index <= 0.90 and
       pct_codes_below_median >= 0.60; HIGH_COST if weighted_cost_index >= 1.10 and pct_codes_below_median <= 0.40;
       else AVERAGE.
Price only: no quality or utilization data is available in PTD, so this measures negotiated price levels.

Output: data/stage7/provider_cost_effectiveness.parquet (+ .csv), data/stage7/code_rate_index.parquet
Usage : python -m src.stage7_cost_effectiveness
"""
from __future__ import annotations

import pandas as pd

from src.common.normalize import load_config

MIN_PEERS, MIN_CODES = 5, 3


def main():
    cfg = load_config()
    s6, s7 = cfg["paths"]["stage6"], cfg["paths"]["stage7"]
    s7.mkdir(parents=True, exist_ok=True)
    f = pd.read_parquet(s6 / "provider_rate_file.parquet")
    sp = pd.read_excel(cfg["paths"]["hilabs_xlsx"], sheet_name="Spec-Proc Weights", dtype=str)
    w = dict(zip(zip(sp.SPEC_CD, sp.PROC_CD), sp.SPEC_PROC_WEIGHT.astype(float)))

    # one rate per provider x code (addresses only repeat the same rate); county from the first address
    base = (f.dropna(subset=["NPI", "spec_cd"]).sort_values("address_tier")
            .groupby(["NPI", "TIN", "Billing Code"]).agg(rate=("Rate", "first"), spec_cd=("spec_cd", "first"),
                                                         specialty=("Specialty", "first"),
                                                         county=("county_fips5", "first")).reset_index())
    base["county"] = base.county.fillna("UNKNOWN")
    cnt = base.groupby(["Billing Code", "spec_cd", "county"]).NPI.transform("nunique")
    med_cty = base.groupby(["Billing Code", "spec_cd", "county"]).rate.transform("median")
    cnt_st = base.groupby(["Billing Code", "spec_cd"]).NPI.transform("nunique")
    med_st = base.groupby(["Billing Code", "spec_cd"]).rate.transform("median")
    use_cty = (cnt >= MIN_PEERS) & base.county.ne("UNKNOWN")
    base["peer_scope"] = use_cty.map({True: "county", False: "statewide"})
    base["peer_n"] = cnt.where(use_cty, cnt_st)
    base["peer_median"] = med_cty.where(use_cty, med_st)
    base = base[base.peer_n >= MIN_PEERS].copy()
    base["rate_index"] = base.rate / base.peer_median
    base["w"] = [w.get((s, c), 0.0) for s, c in zip(base.spec_cd, base["Billing Code"])]
    base["w"] = base.w.where(base.w > 0, base.w[base.w > 0].min() if (base.w > 0).any() else 1.0)  # unlisted code: smallest weight
    base.to_parquet(s7 / "code_rate_index.parquet", index=False)

    g = base.groupby(["NPI", "TIN"])
    prov = g.apply(lambda d: pd.Series({
        "specialty": d.specialty.iloc[0], "spec_cd": d.spec_cd.iloc[0], "county": d.county.iloc[0],
        "n_codes_compared": len(d),
        "weighted_cost_index": (d.w * d.rate_index).sum() / d.w.sum(),
        "pct_codes_below_median": (d.rate < d.peer_median).mean(),
        "share_county_peers": (d.peer_scope == "county").mean()}), include_groups=False).reset_index()
    prov["label"] = "AVERAGE"
    prov.loc[(prov.weighted_cost_index <= 0.90) & (prov.pct_codes_below_median >= 0.60), "label"] = "COST_EFFECTIVE"
    prov.loc[(prov.weighted_cost_index >= 1.10) & (prov.pct_codes_below_median <= 0.40), "label"] = "HIGH_COST"
    prov.loc[prov.n_codes_compared < MIN_CODES, "label"] = "INSUFFICIENT_DATA"
    prov = prov.round({"weighted_cost_index": 3, "pct_codes_below_median": 3, "share_county_peers": 3})
    prov.to_parquet(s7 / "provider_cost_effectiveness.parquet", index=False)
    prov.to_csv(s7 / "provider_cost_effectiveness.csv", index=False, encoding="utf-8")
    print(f"providers scored: {len(prov):,} | code comparisons: {len(base):,} | peer scope: "
          f"{base.peer_scope.value_counts().to_dict()}")
    print("labels:", prov.label.value_counts().to_dict())
    print("weighted_cost_index quantiles:", prov[prov.label != 'INSUFFICIENT_DATA'].weighted_cost_index
          .quantile([.1, .25, .5, .75, .9]).round(3).to_dict())


if __name__ == "__main__":
    main()
