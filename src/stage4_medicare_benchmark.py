"""C4 add-on: is each extracted rate plausible? Benchmark against Medicare and against peers on the same code.

A negotiated rate on its own says little: $104.88 for 99213 is only meaningful next to what the same service costs
elsewhere. Commercial contracts are conventionally expressed as a percentage of Medicare, so every rate gets:

  medicare_rate     professional rows: 2026 Medicare Physician Fee Schedule for the provider's OWN California
                    locality = (work RVU x work GPCI + PE RVU x PE GPCI + MP RVU x MP GPCI) x conversion factor.
                    PE RVU = facility PE when every place of service on the row is a facility setting (hospital,
                    ASC, SNF...), else non-facility PE. Modifier 26 / TC rows use the CMS 26 / TC lines.
                    facility rows: OPPS national payment rate, October 2026 quarterly Addendum B for the CPT/HCPCS code. It is NOT
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

Data currency (is the Medicare data itself stale?): the PFS file is the latest 2026 quarter (October) and OPPS the
October 2026 quarterly update. The report re-tests it every run: Health Net's own professional rates are compared with
the 2026 and the 2025 PFS for the same locality; a contract written as "X% of Medicare" lands on a whole-number
percentage only for the schedule year it was built on (chance alone: ~10% of rows).

Inputs : data/stage4/rates.parquet, data/stage5/npi_tin_address.parquet, CMS zips (config sources.cms_*)
Output : columns added to data/stage4/rates.parquet (+ .csv); data/stage4/c4_medicare_benchmark.txt
Usage  : python -m src.stage4_medicare_benchmark
"""
from __future__ import annotations

import csv
import io
import re
import zipfile

import numpy as np
import pandas as pd

from src.common.normalize import file_name_from_url, load_config, save_csv
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


def cms_paths(cfg) -> dict:
    """Local file of every CMS source = the file name in its config URL (no file name is fixed in code)."""
    P, S = cfg["paths"], cfg["sources"]
    return {"pfs": P["cms_pfs"] / file_name_from_url(S["cms_pfs_rvu_zip"]),
            "prior": P["cms_pfs_prior"] / file_name_from_url(S["cms_pfs_prior_zip"]),
            "opps": P["cms_opps"] / file_name_from_url(S["cms_opps_addenda_zip"])}


def pfs_year(path) -> str:
    """Schedule year from the CMS RVU file inside the zip (PPRRVU<year>_...)."""
    n = next(x for x in zipfile.ZipFile(path).namelist() if x.startswith("PPRRVU"))
    return re.search(r"PPRRVU(\d{4})", n).group(1)


def opps_label(path) -> str:
    n = next(x for x in zipfile.ZipFile(path).namelist() if "Addendum B" in x.replace("_", " "))
    m = re.search(r"(\d{4})[ _]([A-Za-z]+)[ _]Web", n) or re.search(r"(\d{4}) NFRM", n)
    return f"{m.group(2)} {m.group(1)}" if m and m.lastindex == 2 else (f"{m.group(1)} final rule" if m else n)


def load_pfs(path):
    z = zipfile.ZipFile(path)
    rvs = [n for n in z.namelist() if n.startswith("PPRRVU") and n.endswith(".csv")]
    rv = next((n for n in rvs if "nonQPP" in n), rvs[0])    # 2026+: non-QPP conversion factor; earlier years: one file
    rows = _rows(z, rv)
    h = next(i for i, r in enumerate(rows) if r and r[0] == "HCPCS")
    # header is spread over 5 lines and CMS moves columns between years (2026 added PRIC IND): find them by name
    hdr = [" ".join(x[j].strip() for x in rows[h - 4:h + 1] if j < len(x)).strip() for j in range(len(rows[h]))]
    col = {k: hdr.index(v) for k, v in {"st": "STATUS CODE", "w": "WORK RVU", "nf": "NON-FAC PE RVU",
                                         "f": "FACILITY PE RVU", "mp": "MP RVU", "cf": "CONV FACTOR"}.items()}
    pfs = pd.DataFrame([(r[0], r[1], r[col["st"]], _num(r[col["w"]]), _num(r[col["nf"]]), _num(r[col["f"]]),
                         _num(r[col["mp"]]), _num(r[col["cf"]])) for r in rows[h + 1:] if r and r[0]],
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
    """Addendum B: the annual rule (.csv) or a quarterly update (.txt, tab-separated); columns found by name."""
    z = zipfile.ZipFile(path)
    n = next(x for x in z.namelist() if "Addendum B" in x.replace("_", " ") and x.endswith((".csv", ".txt")))
    rows = list(csv.reader(io.StringIO(z.read(n).decode("latin-1")), delimiter="	" if n.endswith(".txt") else ","))
    h = next(i for i, r in enumerate(rows) if r and r[0].strip() == "HCPCS Code")
    hdr = [x.strip() for x in rows[h]]
    si, pr = hdr.index("SI"), hdr.index("Payment Rate")
    o = pd.DataFrame([(r[0].strip(), r[si].strip(), _num(r[pr])) for r in rows[h + 1:] if r and r[0].strip()],
                     columns=["code", "si", "opps_rate"])
    return o[o.opps_rate > 0]


def main():
    cfg = load_config()
    P, S = cfg["paths"], cfg["sources"]
    cp = cms_paths(cfg)
    pfs_zip, opps_zip = cp["pfs"], cp["opps"]
    for url, dest in ((S["cms_pfs_rvu_zip"], pfs_zip), (S["cms_opps_addenda_zip"], opps_zip)):
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            download(url, dest, cfg)
    prior_zip = cp["prior"]
    if not prior_zip.exists():
        prior_zip.parent.mkdir(parents=True, exist_ok=True)
        download(S["cms_pfs_prior_zip"], prior_zip, cfg)
    pfs, gp, county_loc = load_pfs(pfs_zip)
    yr, yr0, olab = pfs_year(pfs_zip), pfs_year(prior_zip), opps_label(opps_zip)
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
    idx = pd.MultiIndex.from_arrays([r.billing_code, pm])
    fac_set = r.service_code.str.split("|").map(lambda s: bool(s) and set(s) <= FACILITY_POS)

    def pfs_price(pf, g_w, g_pe, g_mp):
        p = pf.set_index(["code", "mod"])
        pe = np.where(fac_set, p.pe_f.reindex(idx).values, p.pe_nf.reindex(idx).values)
        return (p.work.reindex(idx).values * g_w + pe * g_pe + p.mp.reindex(idx).values * g_mp) * p.cf.reindex(idx).values

    pfs_amt = pfs_price(pfs, gw.values, gpe.values, gmp.values)
    # same locality, 2025 schedule: only for the data-currency test
    pf25, gp25, _ = load_pfs(prior_zip)
    g25, m25 = gp25.set_index("loc"), gp25[["g_work", "g_pe", "g_mp"]].median()
    amt25 = pfs_price(pf25, *(r["loc"].map(g25[c]).fillna(m25[c]).values for c in ("g_work", "g_pe", "g_mp")))
    opps_amt = r.billing_code.map(opps.set_index("code").opps_rate).values
    prof = r.component.eq("professional").values
    r["medicare_rate"] = np.round(np.where(prof, pfs_amt, opps_amt), 2)
    r.loc[r.medicare_rate <= 0, "medicare_rate"] = np.nan
    r["medicare_source"] = np.where(r.medicare_rate.isna(), "", np.where(
        prof, np.where(fac_set, f"PFS {yr} facility, CA locality", f"PFS {yr} non-facility, CA locality"),
        f"OPPS {olab} national (not wage-adjusted)"))
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
    std_p = prof & r.modifier.eq("").values & ~np.isnan(pfs_amt) & ~np.isnan(amt25)
    cur = {}
    for y, amt in ((yr, pfs_amt), (yr0, amt25)):
        pct = 100 * r.rate.values[std_p] / amt[std_p]
        cur[y] = (int((np.abs(pct - 100) <= 0.05).sum()), int((np.abs(pct - np.round(pct)) <= 0.05).sum()))
    n_std = int(std_p.sum())
    r = r.drop(columns=["_k", "loc"])
    r.to_parquet(P["stage4"] / "rates.parquet", index=False)
    save_csv(r, P["stage4"] / "rates.csv")

    # report
    b = r[r.medicare_rate.notna()]
    std = b[b.modifier.eq("")]
    L = ["C4 Medicare benchmark and rate plausibility", "=" * 78,
         f"Sources: CMS {yr} PFS RVU file {pfs_zip.name} ({len(pfs):,} priced code lines, {len(gp)} CA localities, "
         f"CF {pfs.cf.dropna().iloc[0]}); CMS OPPS Addendum B {olab} ({len(opps):,} paid codes); URLs in config.yaml",
         f"Rows with a Medicare benchmark: {len(b):,} of {len(r):,} ({100 * len(b) / len(r):.1f}%)  "
         f"professional {int(b.component.eq('professional').sum()):,}, facility {int(b.component.eq('facility').sum()):,}",
         "   (no benchmark: drugs / supplies priced per unit (J-codes: Medicare ASP), lab codes on the CLFS, packaged codes)",
         f"Locality from the provider's own county: {int(r.medicare_locality.ne('CA median (no county)').sum() - (~r.component.eq('professional')).sum()):,} "
         f"professional rows; CA median GPCI for the rest",
         "",
         "Data currency check (is the Medicare data stale?):",
         f"   PFS = {pfs_zip.name} (schedule year {yr}); OPPS = Addendum B {olab}. Release choice and the check that",
         "   no newer release existed are documented in docs/C1_C2_C4_logic_PPO_network_and_rates.md section 5, step 7.",
         f"   Health Net standard professional rates ({n_std:,} rows) vs the same locality's PFS:",
         f"      {yr}: exactly 100% of Medicare {cur[yr][0]:,} rows; at a whole-number % {cur[yr][1]:,}",
         f"      {yr0}: exactly 100% of Medicare {cur[yr0][0]:,} rows; at a whole-number % {cur[yr0][1]:,}",
         f"      chance alone (+-0.05 of a whole number): ~{round(0.1 * n_std):,} rows",
         (f"   -> Health Net's contracts are written on the {yr} schedule we use: the benchmark is current."
          if cur[yr][1] > cur[yr0][1] else f"   -> WARNING: rates fit the {yr0} schedule better: check the PFS release."),
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
