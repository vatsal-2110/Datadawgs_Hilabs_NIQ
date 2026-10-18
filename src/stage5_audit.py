"""C5 audit: re-derive, on THIS run's providers, the evidence behind the C5 rules.

Every check compares buildings with the shared standardizer (src.common.address.building_key), independent of
the Census geocoder, so the evidence does not depend on the step it is used to justify.

  A  recency      Does a recent NPPES update mean a correct address? Rate at which CMS Doctors & Clinicians
                  (Medicare enrolment) lists the NPI at its NPPES primary building, by NPPES update age.
                  Decides whether "NPPES updated recently" may count as evidence (logic D5.8).
  B  triangulate  For NPIs with NPPES primary (P), DAC (D) and an NPPES endpoint address (E): which source is
                  the odd one out? Tests "DAC is right, NPPES is stale" instead of assuming it.
  C  baseline     The NPPES-only answer (what MCheck/R3 on NPPES gives) next to the pipeline's answer.

Inputs : data/stage4/tin_npi_pairs.parquet, data/stage5/{nppes_npi, nppes_addresses, dac_addresses,
         npi_tin_address}.parquet
Output : data/stage5/c5_audit.json, data/stage5/c5_audit.txt
Usage  : python -m src.stage5_audit
"""
from __future__ import annotations

import json

import pandas as pd

from src.common.address import building_key
from src.common.normalize import load_config

AGE_BUCKETS = [(0, 1, "<1y"), (1, 2, "1-2y"), (2, 5, "2-5y"), (5, 10, "5-10y"), (10, 999, "10y+")]


def _keys(df: pd.DataFrame) -> pd.Series:
    return pd.Series([building_key(a, c, s, z) for a, c, s, z in
                      zip(df.address_1, df.city, df.state, df.zip)], index=df.index)


def recency(nppes, na, dac, as_of) -> dict:
    loc = na[na.purpose == "LOCATION"].assign(k=lambda d: _keys(d))[["npi", "k"]]
    dk = dac.assign(k=_keys(dac)).groupby("npi").k.agg(set)
    t = loc[loc.npi.isin(dk.index) & loc.k.ne("")].merge(nppes[["npi", "last_updated"]], on="npi")
    t["confirmed"] = [k in dk[n] for n, k in zip(t.npi, t.k)]
    t["age_y"] = (as_of - pd.to_datetime(t.last_updated, errors="coerce")).dt.days / 365.25
    rows = []
    for lo, hi, lab in AGE_BUCKETS:
        s = t[(t.age_y >= lo) & (t.age_y < hi)]
        rows.append({"update_age": lab, "npis": int(len(s)),
                     "pct_dac_confirms_nppes_primary": round(100 * s.confirmed.mean(), 1) if len(s) else None})
    cur = t.age_y <= 2
    return {"as_of": as_of.date().isoformat(), "npis_in_dac_with_nppes_primary": int(len(t)),
            "by_update_age": rows,
            "updated_within_24m": {"npis": int(cur.sum()), "pct_confirmed": round(100 * t[cur].confirmed.mean(), 1)},
            "older_than_24m": {"npis": int((~cur).sum()), "pct_confirmed": round(100 * t[~cur].confirmed.mean(), 1)}}


def triangulate(na, dac) -> dict:
    P = na[na.purpose == "LOCATION"].assign(k=lambda d: _keys(d)).groupby("npi").k.first()
    D = dac.assign(k=_keys(dac)).groupby("npi").k.agg(set)
    E = na[na.purpose == "ENDPOINT"].assign(k=lambda d: _keys(d)).groupby("npi").k.agg(set)
    npis = P.index.intersection(D.index).intersection(E.index)
    c = {"all_agree": 0, "nppes_primary_odd_one": 0, "dac_odd_one": 0, "endpoint_odd_one": 0, "all_differ": 0}
    for n in npis:
        pd_, pe, de = P[n] in D[n], P[n] in E[n], bool(D[n] & E[n])
        key = ("all_agree" if pd_ and pe else "endpoint_odd_one" if pd_ else "dac_odd_one" if pe
               else "nppes_primary_odd_one" if de else "all_differ")
        c[key] += 1
    return {"npis_with_all_three": int(len(npis)), **c}


def baseline_vs_pipeline(pairs, na, addr) -> dict:
    npis = pairs.npi.dropna().unique()
    loc = na[(na.purpose == "LOCATION") & na.npi.isin(npis)]
    a = addr[addr.npi.notna()].copy()
    a["has_loc"] = a.sources.fillna("").str.contains("NPPES_LOC")
    ver = set(a[a.tier == "VERIFIED"].npi)
    loc_ver = set(a[a.has_loc & (a.tier == "VERIFIED")].npi)
    best = a[a.is_best.fillna(False).astype(bool)]
    best_not_loc = best[~best.has_loc & best.tier.isin(["VERIFIED", "PROBABLE"])]
    n = len(npis)
    return {
        "npis": int(n),
        "baseline_nppes_primary_found": int(loc.npi.nunique()),
        "baseline_nppes_primary_independently_confirmed": int(len(loc_ver)),
        "pct_baseline_confirmed": round(100 * len(loc_ver) / n, 1),
        "pipeline_npis_verified": int(len(ver)),
        "pct_pipeline_verified": round(100 * len(ver) / n, 1),
        "verified_only_at_non_nppes_primary_address": int(len(ver - loc_ver)),
        "pairs_best_address_not_nppes_primary": int(len(best_not_loc)),
    }


def main():
    cfg = load_config()
    s5 = cfg["paths"]["stage5"]
    pairs = pd.read_parquet(cfg["paths"]["stage4"] / "tin_npi_pairs.parquet")
    nppes = pd.read_parquet(s5 / "nppes_npi.parquet")
    na = pd.read_parquet(s5 / "nppes_addresses.parquet")
    dac = pd.read_parquet(s5 / "dac_addresses.parquet").rename(
        columns={"adr_ln_1": "address_1", "adr_ln_2": "address_2", "citytown": "city"})
    addr = pd.read_parquet(s5 / "npi_tin_address.parquet")
    # as-of = newest NPPES update in the fetched data (not the clock), so re-runs give the same numbers
    as_of = pd.to_datetime(nppes.last_updated, errors="coerce").max()

    out = {"A_recency": recency(nppes, na, dac, as_of),
           "B_triangulation": triangulate(na, dac),
           "C_baseline_vs_pipeline": baseline_vs_pipeline(pairs, na, addr)}
    (s5 / "c5_audit.json").write_text(json.dumps(out, indent=2), encoding="utf-8")

    r, t, b = out["A_recency"], out["B_triangulation"], out["C_baseline_vs_pipeline"]
    L = ["C5 audit (evidence behind the C5 rules, re-derived on this run's providers)", "=" * 78,
         f"A. Does a recent NPPES update mean a correct address?  (as-of {r['as_of']}; "
         f"{r['npis_in_dac_with_nppes_primary']:,} NPIs with an NPPES primary address and a Medicare record)",
         *[f"   updated {x['update_age']:>6}: {x['npis']:>5,} NPIs, Medicare confirms the NPPES primary building "
           f"{x['pct_dac_confirms_nppes_primary']}%" for x in r["by_update_age"]],
         f"   within 24 months: {r['updated_within_24m']['pct_confirmed']}% (n={r['updated_within_24m']['npis']:,})"
         f"  vs older: {r['older_than_24m']['pct_confirmed']}% (n={r['older_than_24m']['npis']:,})",
         "",
         f"B. Which source is the odd one out? ({t['npis_with_all_three']:,} NPIs with NPPES primary + Medicare + endpoint)",
         f"   all agree {t['all_agree']} | NPPES primary odd one {t['nppes_primary_odd_one']} | Medicare odd one "
         f"{t['dac_odd_one']} | endpoint odd one {t['endpoint_odd_one']} | all differ {t['all_differ']}",
         "",
         f"C. NPPES-only baseline vs this pipeline ({b['npis']:,} NPIs)",
         f"   NPPES primary address found: {b['baseline_nppes_primary_found']:,}; independently confirmed: "
         f"{b['baseline_nppes_primary_independently_confirmed']:,} ({b['pct_baseline_confirmed']}%)",
         f"   pipeline: NPIs with a VERIFIED address {b['pipeline_npis_verified']:,} ({b['pct_pipeline_verified']}%), "
         f"of which {b['verified_only_at_non_nppes_primary_address']:,} only at an address that is NOT their NPPES primary",
         f"   (NPI, TIN) pairs whose best address is not the NPPES primary: {b['pairs_best_address_not_nppes_primary']:,}"]
    (s5 / "c5_audit.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
