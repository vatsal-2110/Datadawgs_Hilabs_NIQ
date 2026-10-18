"""C4 add-on: is each extracted rate plausible? Benchmark against Medicare and against peers on the same code.

A negotiated rate on its own says little: $104.88 for 99213 is only meaningful next to what the same service costs
elsewhere. Commercial contracts are conventionally expressed as a percentage of Medicare, so every rate gets:

  medicare_rate     professional rows: 2026 Medicare Physician Fee Schedule for the provider's OWN California
                    locality = (work RVU x work GPCI + PE RVU x PE GPCI + MP RVU x MP GPCI) x conversion factor.
                    PE RVU = facility PE when every place of service on the row is a facility setting (hospital,
                    ASC, SNF...), else non-facility PE. Modifier 26 / TC rows use the CMS 26 / TC lines.
                    facility rows: 2026 OPPS national payment rate (Addendum B) for the CPT/HCPCS code. It is NOT
                    wage-adjusted, so CA facility percentages are overstated by roughly the CA wage index; read
                    them as a relative scale, not an exact multiple. Finding: within one hospital the ratio to OPPS
                    swings widely and cheap services carry case-sized prices (foot X-ray 73630 ~$4,900), so the
                    file's facility amounts behave like encounter-level prices, not line-item fees. They are kept,
                    labelled `facility`, and never compared with professional fees.
  pct_of_medicare   rate / medicare_rate x 100
  medicare_locality locality used (from the county of the pair's best address; "CA median (no county)" otherwise)
  peer_robust_z     0.6745 x (log rate - median log rate) / MAD over rows of the same code, component and modifier
                    (groups of >= 5 rows): the standard robust (median/MAD) outlier score
  rate_check        OK, or why the rate looks wrong: HIGH_VS_PEERS / LOW_VS_PEERS (|z| > 3.5),
                    ABOVE_10X_MEDICARE / BELOW_30PCT_MEDICARE. Flagged rows are kept (they are real file values),
                    so users can filter them out of benchmarks.

Inputs : data/stage4/rates.parquet, data/stage5/npi_tin_address.parquet, CMS zips (config sources.cms_*)
Output : columns added to data/stage4/rates.parquet (+ .csv); data/stage4/c4_medicare_benchmark.txt
Usage  : python -m src.stage4_medicare_benchmark
"""
from __future__ import annotations

import csv
import io
import zipfile

import numpy as np
import pandas as pd

from src.common.normalize import load_config
from src.stage1_ingest_index import download
from src.stage2_provider_network import CA_COUNTY_FIPS

FACILITY_POS = {"19", "21", "22", "23", "24", "26", "31", "34", "51", "52", "53", "56", "61"}
BENCH_COLS = ["medicare_rate", "medicare_source", "medicare_locality", "pct_of_medicare", "peer_robust_z", "rate_check"]
Z_MAX, MIN_GROUP = 3.5, 5


def _rows(z: zipfile.ZipFile, name: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(z.read(name).decode("latin-1"))))


def _num(x) -> float:
    try:
        return float(str(x).replace("$", "").replace(",", ""))
    except ValueError:
        return np.nan


def load_pfs(path):
    z = zipfile.ZipFile(path)
    rv = next(n for n in z.namelist() if n.startswith("PPRRVU") and "nonQPP" in n and n.endswith(".csv"))
    rows = _rows(z, rv)
    h = next(i for i, r in enumerate(rows) if r and r[0] == "HCPCS")
    # columns (fixed CMS layout): 0 HCPCS 1 MOD 3 STATUS 5 WORK 6 NF PE 8 F PE 10 MP 25 CONV FACTOR
    pfs = pd.DataFrame([(r[0], r[1], r[3], _num(r[5]), _num(r[6]), _num(r[8]), _num(r[10]), _num(r[25]))
                        for r in rows[h + 1:] if r and r[0]],
                       columns=["code", "mod", "status", "work", "pe_nf", "pe_f", "mp", "cf"])
    pfs = pfs[pfs.status.isin(["A", "R", "T"])]          # actively priced by the PFS
    g = _rows(z, next(n for n in z.namelist() if n.startswith("GPCI") and n.endswith(".csv")))
    gp = pd.DataFrame([(r[2], r[3], _num(r[4]), _num(r[5]), _num(r[6])) for r in g if len(r) > 6 and r[1] == "CA"],
                      columns=["loc", "locality", "g_work", "g_pe", "g_mp"])
    lc = _rows(z, next(n for n in z.namelist() if n.endswith("LOCCO.csv")))
    county_loc, on = {}, False
    for r in lc:
        if len(r) > 4 and r[2].strip() == "CALIFORNIA":
            on = True
        elif len(r) > 4 and r[2].strip() and on:
            break
        if on and len(r) > 4 and r[1].strip():
            for c in r[4].replace("/", ",").split(","):
                c = c.replace("CNTY", "").replace("ORAGNGE", "ORANGE").strip().title()   # CMS file typo "ORAGNGE"
                if c:
                    county_loc[c] = r[1].strip()
    return pfs, gp, county_loc


def load_opps(path):
    z = zipfile.ZipFile(path)
    n = next(x for x in z.namelist() if "Addendum B" in x and x.endswith(".csv"))
    rows = _rows(z, n)
    h = next(i for i, r in enumerate(rows) if r and r[0].strip() == "HCPCS Code")
    o = pd.DataFrame([(r[0].strip(), r[3].strip(), _num(r[6])) for r in rows[h + 1:] if r and r[0].strip()],
                     columns=["code", "si", "opps_rate"])
    return o[o.opps_rate > 0]


def main():
    cfg = load_config()
    P, S = cfg["paths"], cfg["sources"]
    pfs_zip, opps_zip = P["cms_pfs"] / "rvu26d.zip", P["cms_opps"] / "opps2026_addenda.zip"
    for url, dest in ((S["cms_pfs_rvu_zip"], pfs_zip), (S["cms_opps_addenda_zip"], opps_zip)):
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            download(url, dest, cfg)
    pfs, gp, county_loc = load_pfs(pfs_zip)
    opps = load_opps(opps_zip)

    r = pd.read_parquet(P["stage4"] / "rates.parquet")
    r = r.drop(columns=[c for c in BENCH_COLS if c in r.columns])
    # locality of each (npi, tin): county of the best address
    a = pd.read_parquet(P["stage5"] / "npi_tin_address.parquet")
    a = a[a.is_best & a.county_fips5.fillna("").str.startswith("06")]
    a["county"] = a.county_fips5.str[2:].map(CA_COUNTY_FIPS)
    a["loc"] = a.county.map(county_loc).fillna("75")      # CMS: every unlisted CA county = 75 "Rest of State"
    r["_k"] = r.npi.fillna("") + "|" + r.tin
    loc = dict(zip(a.npi.fillna("") + "|" + a.tin, a["loc"]))
    r["loc"] = r._k.map(loc)
    gmed = gp[["g_work", "g_pe", "g_mp"]].median()
    g = gp.set_index("loc")
    r["medicare_locality"] = r["loc"].map(g.locality).fillna("CA median (no county)")
    gw, gpe, gmp = (r["loc"].map(g[c]).fillna(gmed[c]) for c in ("g_work", "g_pe", "g_mp"))

    # professional: PFS line for code + 26/TC modifier
    pm = r.modifier.str.split("|").map(lambda m: "26" if "26" in m else ("TC" if "TC" in m else ""))
    p = pfs.set_index(["code", "mod"])
    idx = pd.MultiIndex.from_arrays([r.billing_code, pm])
    fac_set = r.service_code.str.split("|").map(lambda s: bool(s) and set(s) <= FACILITY_POS)
    pe = np.where(fac_set, p.pe_f.reindex(idx).values, p.pe_nf.reindex(idx).values)
    pfs_amt = (p.work.reindex(idx).values * gw.values + pe * gpe.values + p.mp.reindex(idx).values * gmp.values) \
        * p.cf.reindex(idx).values
    opps_amt = r.billing_code.map(opps.set_index("code").opps_rate).values
    prof = r.component.eq("professional").values
    r["medicare_rate"] = np.round(np.where(prof, pfs_amt, opps_amt), 2)
    r.loc[r.medicare_rate <= 0, "medicare_rate"] = np.nan
    r["medicare_source"] = np.where(r.medicare_rate.isna(), "", np.where(
        prof, np.where(fac_set, "PFS 2026 facility, CA locality", "PFS 2026 non-facility, CA locality"),
        "OPPS 2026 national (not wage-adjusted)"))
    r.loc[~prof, "medicare_locality"] = "national (OPPS)"
    r["pct_of_medicare"] = (100 * r.rate / r.medicare_rate).round(1)

    # robust peer outlier score on log rates
    lr = np.log(r.rate)
    grp = r.groupby(["billing_code", "component", "modifier"])
    med = lr.groupby([r.billing_code, r.component, r.modifier]).transform("median")
    mad = (lr - med).abs().groupby([r.billing_code, r.component, r.modifier]).transform("median")
    n = grp.rate.transform("size")
    z = 0.6745 * (lr - med) / mad.replace(0, np.nan)
    r["peer_robust_z"] = z.where(n >= MIN_GROUP).round(2)
    chk = [[] for _ in range(len(r))]
    for i in np.where(r.peer_robust_z > Z_MAX)[0]:
        chk[i].append("HIGH_VS_PEERS")
    for i in np.where(r.peer_robust_z < -Z_MAX)[0]:
        chk[i].append("LOW_VS_PEERS")
    # Medicare limits only on professional rows: facility amounts behave like encounter-level prices (see report)
    for i in np.where(prof & (r.pct_of_medicare > 1000))[0]:
        chk[i].append("ABOVE_10X_MEDICARE")
    for i in np.where(prof & (r.pct_of_medicare < 30))[0]:
        chk[i].append("BELOW_30PCT_MEDICARE")
    r["rate_check"] = ["; ".join(c) or "OK" for c in chk]
    r = r.drop(columns=["_k", "loc"])
    r.to_parquet(P["stage4"] / "rates.parquet", index=False)
    r.to_csv(P["stage4"] / "rates.csv", index=False, encoding="utf-8")

    # report
    b = r[r.medicare_rate.notna()]
    std = b[b.modifier.eq("")]
    L = ["C4 Medicare benchmark and rate plausibility", "=" * 78,
         f"Sources: CMS 2026 PFS RVU file (October release, {len(pfs):,} priced code lines, {len(gp)} CA localities, "
         f"CF {pfs.cf.dropna().iloc[0]}); CMS CY2026 OPPS Addendum B ({len(opps):,} paid codes)",
         f"Rows with a Medicare benchmark: {len(b):,} of {len(r):,} ({100 * len(b) / len(r):.1f}%)  "
         f"professional {int(b.component.eq('professional').sum()):,}, facility {int(b.component.eq('facility').sum()):,}",
         "   (no benchmark: drugs / supplies priced per unit (J-codes: Medicare ASP), lab codes on the CLFS, packaged codes)",
         f"Locality from the provider's own county: {int(r.medicare_locality.ne('CA median (no county)').sum() - (~r.component.eq('professional')).sum()):,} "
         f"professional rows; CA median GPCI for the rest",
         "",
         "Rate as % of Medicare, standard service (no modifier), median [p25-p75]:"]
    for c, d in std.groupby("component"):
        q = d.pct_of_medicare.quantile([.25, .5, .75])
        L.append(f"   {c:<12} {q[.5]:6.0f}%  [{q[.25]:.0f}% - {q[.75]:.0f}%]   rows {len(d):,}")
    fh = std[std.component.eq("facility")].groupby(["npi", "tin"]).pct_of_medicare.agg(
        n="size", logsd=lambda s: float(np.log(s).std()))
    fh = fh[fh.n >= 5]
    pr = std[std.component.eq("professional")]
    L += ["", f"Professional fee schedule is Medicare-pegged: {100 * ((pr.pct_of_medicare - 100).abs() <= 1).mean():.1f}% of "
              f"standard professional rows are within 1% of the provider's own-locality Medicare rate; common contract "
              f"levels: " + ", ".join(f"{int(k)}% ({v:,})" for k, v in pr.pct_of_medicare.round(-1).value_counts().head(6).items()),
          f"Facility amounts are NOT a fixed % of Medicare: within one hospital (>= 5 codes, {len(fh)} hospitals) the "
          f"log-sd of rate/OPPS is {fh.logsd.median():.2f} (0 = one multiple); e.g. foot X-ray 73630 median "
          f"${std[std.billing_code.eq('73630') & std.component.eq('facility')].rate.median():,.0f}. They behave like "
          f"encounter-level prices: kept, labelled facility, never compared with professional fees."]
    top = std[std.component.eq("professional")].groupby("billing_code").agg(
        rows=("rate", "size"), median_rate=("rate", "median"), medicare=("medicare_rate", "median"),
        pct=("pct_of_medicare", "median")).sort_values("rows", ascending=False).head(10)
    L += ["", "Most-priced professional codes:", top.round(1).to_string(), "",
          "rate_check (rows; flagged rows are kept, not dropped):"]
    vc = r.rate_check.str.split("; ").explode().value_counts()
    L += [f"   {k:<22} {v:,}" for k, v in vc.items()]
    ex = r[r.rate_check.ne("OK")].sort_values("pct_of_medicare", ascending=False)
    L += ["", "Examples of flagged rows:",
          ex[["npi", "tin", "billing_code", "component", "modifier", "rate", "medicare_rate", "pct_of_medicare",
              "peer_robust_z", "rate_check"]].head(8).to_string(index=False)]
    (P["stage4"] / "c4_medicare_benchmark.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
