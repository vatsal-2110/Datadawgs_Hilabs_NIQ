"""C5 step 2: map every (NPI, TIN) in the PTD file to practicing address(es), scored with evidence.

Candidate sources per (NPI, TIN)       (fetched by src.stage5_fetch_sources)
  NPPES_LOC   NPPES primary practice LOCATION
  NPPES_SEC   NPPES secondary practice locations
  NPPES_ENDPOINT  NPPES endpoint affiliation address (same NPPES family: can't confirm NPPES by itself)
  DAC         CMS Doctors & Clinicians: Medicare-enrolled practice addresses (refreshed monthly)
  TIN_SIBLING for TIN-only rows (no NPI): addresses of the other NPIs under the same TIN
(NPPES MAILING addresses are NOT practice locations and are never used as candidates.)

Standardization: every address is standardized with the shared rules (src/common/address.py), then sent to the
US Census geocoder (batch API), which returns a standardized address, Match / No_Match, coordinates and county
FIPS. Addresses are compared at building level (standardized street + ZIP5); suites are kept for display only.

Evidence per candidate (npi, tin, address):
  n_independent_sources   families agreeing on the building: NPPES, CMS enrolment (DAC), CMS facility registry
  tin_corroboration       another NPI billing under the SAME TIN in the PTD file practices at this address
  dac_name_match          DAC group (facility_name) shares >= 50% of name tokens with the PTD business name
  geocoded                Census Match (the address exists)
  nppes_current           reported only: NPPES update age does not predict a correct address (stage5_audit, A)

Tiers (logic D5.5, D5.8):
  VERIFIED   geocoded AND (n_independent_sources >= 2 OR (1 source AND (tin_corroboration OR dac_name_match)))
  PROBABLE   geocoded AND one non-NPPES source (DAC individual or group, or a CMS facility registry)
  UNVERIFIED everything else (NPPES-only, not geocoded, or a PO box): kept, labelled
Ranking inside a pair: tier, then evidence points; is_best marks the top address.

Outputs (data/stage5/): npi_tin_address.parquet (+ .csv), geocode.parquet, c5_report.txt
Usage: python -m src.stage5_address_map
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pandas as pd
import requests

from src.common.address import building_key, standardize_address
from src.common.normalize import load_config, save_csv

STOP = {"INC", "LLC", "MD", "M", "D", "THE", "OF", "A", "PC", "CORP", "CORPORATION", "GROUP", "MEDICAL", "ASSOCIATES",
        "PLLC", "LTD", "CO", "AND", "&", "CENTER", "HEALTH"}


def norm_street(s) -> str:
    """Street line sent to the geocoder: standardized, suite removed (shared rules, src/common/address.py)."""
    return standardize_address(s).street or ""


def raw_key(a1, city, state, z):
    """Building identity for every cross-source comparison (registry, DAC group, TIN corroboration, and
    addresses the Census geocoder can't match): standardized street key + ZIP5 (logic D5.7)."""
    return building_key(a1, city, state, z)


def name_tokens(s):
    s = s if isinstance(s, str) else ""
    return {w for w in re.sub(r"[^A-Z0-9 ]", " ", s.upper()).split() if w not in STOP and len(w) > 1}


def geocode(addresses: pd.DataFrame, cfg) -> pd.DataFrame:
    """addresses: columns key, a1, city, state, zip. Census batch geocoder with per-batch cache."""
    cache = cfg["paths"]["cache"] / "census"
    cache.mkdir(parents=True, exist_ok=True)
    n = cfg["http_pool"]["census_batch"]
    addresses = addresses.drop_duplicates("key").sort_values("key").reset_index(drop=True)
    chunks = [addresses.iloc[i:i + n] for i in range(0, len(addresses), n)]

    def one(chunk):
        h = hashlib.sha1("\n".join(chunk.key).encode()).hexdigest()[:16]
        f = cache / f"{h}.csv"
        if not f.exists():
            body = io.StringIO()
            for i, r in enumerate(chunk.itertuples()):
                vals = [str(i), norm_street(r.a1), r.city or "", r.state or "", (r.zip or "")[:5]]
                body.write(",".join('"' + v.replace('"', "") + '"' for v in vals) + "\n")
            for t in range(4):
                try:
                    resp = requests.post(cfg["sources"]["census_batch"],
                                         files={"addressFile": ("a.csv", body.getvalue(), "text/csv")},
                                         data={"benchmark": cfg["sources"]["census_benchmark"],
                                               "vintage": cfg["sources"]["census_vintage"]}, timeout=600)
                    if resp.ok and resp.text.strip():
                        f.write_text(resp.text, encoding="utf-8")
                        break
                except requests.RequestException:
                    pass
                time.sleep(5 * (t + 1))
            else:
                raise RuntimeError("Census geocoder failed for a batch")
        res = pd.read_csv(f, header=None, dtype=str, names=list(range(12)), engine="python", on_bad_lines="skip")
        res = res.rename(columns={0: "i", 2: "match", 3: "match_type", 4: "std_address", 5: "lonlat",
                                  8: "state_fips", 9: "county_fips", 10: "tract"})
        res["i"] = res.i.astype(int)
        return chunk.reset_index(drop=True).join(res.set_index("i")[["match", "match_type", "std_address", "lonlat",
                                                                     "state_fips", "county_fips", "tract"]])

    print(f"geocoding {len(addresses):,} distinct addresses in {len(chunks)} batches (parallel)")
    with ThreadPoolExecutor(min(4, len(chunks) or 1)) as ex:
        out = pd.concat(list(ex.map(one, chunks)), ignore_index=True)
    out["geocoded"] = out.match.eq("Match")
    ll = out.lonlat.str.split(",", expand=True)
    out["lon"], out["lat"] = pd.to_numeric(ll[0], errors="coerce"), pd.to_numeric(ll[1], errors="coerce")
    out["county_fips5"] = (out.state_fips.fillna("") + out.county_fips.fillna("")).replace("", None)
    return out


def main():
    cfg = load_config()
    s5 = cfg["paths"]["stage5"]
    pairs = pd.read_parquet(cfg["paths"]["stage4"] / "tin_npi_pairs.parquet")
    nppes = pd.read_parquet(s5 / "nppes_npi.parquet")
    na = pd.read_parquet(s5 / "nppes_addresses.parquet")
    dac = pd.read_parquet(s5 / "dac_addresses.parquet")
    today = date.today()
    fresh_days = cfg["address"]["nppes_fresh_months"] * 30.44
    # as-of = newest NPPES update in the fetched data, not the clock, so a re-run gives the same result
    as_of = pd.to_datetime(nppes.last_updated, errors="coerce").max()

    # ---------------- candidates
    na = na[na.purpose.isin(["LOCATION", "PRACTICE", "ENDPOINT"])].copy()
    na["source"] = na.purpose.map({"LOCATION": "NPPES_LOC", "PRACTICE": "NPPES_SEC", "ENDPOINT": "NPPES_ENDPOINT"})
    na["facility_name"] = None
    d = dac.rename(columns={"adr_ln_1": "address_1", "adr_ln_2": "address_2", "citytown": "city"})
    d = d.assign(source="DAC")[["npi", "source", "address_1", "address_2", "city", "state", "zip", "facility_name"]]
    cand = pd.concat([na[["npi", "source", "address_1", "address_2", "city", "state", "zip", "facility_name"]], d])
    cand = pairs[["npi", "tin", "business_names", "npi_missing"]].merge(cand, on="npi", how="left")
    # DAC_GROUP: Medicare group practice addresses whose group name EXACTLY equals the organization's NPPES name
    # or its business name in the rate file (organizations are not in DAC as individuals)
    gpath = s5 / "dac_group_addresses.parquet"
    if gpath.exists():
        grp_rows = pd.read_parquet(gpath)
        if len(grp_rows):
            grp_rows = grp_rows.rename(columns={"adr_ln_1": "address_1", "adr_ln_2": "address_2", "citytown": "city"})
            grp_rows["gname"] = grp_rows.facility_name.str.upper().str.strip()
            grp_rows = grp_rows.drop_duplicates(["gname", "address_1", "city", "zip"])
            orgs = nppes[nppes.enumeration_type == "NPI-2"][["npi", "name"]]
            names = pairs[["npi", "tin", "business_names", "npi_missing"]].merge(orgs, on="npi")
            names = pd.concat([names.assign(gname=names.name.str.upper().str.strip()),
                               names.assign(gname=names.business_names.str.split(" | ", regex=False)).explode("gname")
                               .assign(gname=lambda x: x.gname.str.upper().str.strip())])
            g2 = names.drop(columns="name").drop_duplicates().merge(
                grp_rows[["gname", "address_1", "city", "state", "zip"]], on="gname")
            # CONFIRMATION ONLY: a big group (e.g. 194 Medicare sites) must not hand every site to every one of
            # its organization NPIs; a group site only confirms an address the NPI itself reports elsewhere
            group_keys = defaultdict(set)
            for npi_, tin_, a1, c_, s_, z_ in g2[["npi", "tin", "address_1", "city", "state", "zip"]].values:
                group_keys[(npi_, tin_)].add(raw_key(a1, c_, s_, z_))
        else:
            group_keys = {}
    else:
        group_keys = {}
    # TIN-only rows: borrow candidate addresses of other NPIs on the same TIN
    tin_only = pairs[pairs.npi_missing][["npi", "tin", "business_names", "npi_missing"]]
    sib = tin_only.drop(columns="npi").merge(cand.dropna(subset=["address_1"])
                                             .drop(columns=["business_names", "npi_missing"]), on="tin")
    sib = sib.assign(npi=None, source="TIN_SIBLING")
    cand = pd.concat([cand[~cand.npi_missing], sib, tin_only[~tin_only.tin.isin(sib.tin)]], ignore_index=True)
    has = cand.address_1.notna()
    cand.loc[has, "key"] = [raw_key(*x) for x in cand.loc[has, ["address_1", "city", "state", "zip"]].values]

    # ---------------- standardize + validate
    g = geocode(cand.loc[has, ["key", "address_1", "city", "state", "zip"]].rename(columns={"address_1": "a1"}), cfg)
    g.to_parquet(s5 / "geocode.parquet", index=False)
    cand = cand.merge(g[["key", "geocoded", "match_type", "std_address", "lat", "lon", "county_fips5", "tract"]],
                      on="key", how="left")
    cand["geocoded"] = cand.geocoded.fillna(False).astype(bool)
    cand["addr_id"] = cand.std_address.where(cand.geocoded, cand.key)   # building-level identity

    # ---------------- evidence
    nppes["age_days"] = (as_of - pd.to_datetime(nppes.last_updated, errors="coerce")).dt.days
    cur = dict(zip(nppes.npi, nppes.age_days <= fresh_days))
    cand["nppes_current"] = cand.npi.map(cur).fillna(False).astype(bool)
    c = cand[cand.address_1.notna() & cand.source.notna()]   # rows without any address are handled as NONE below
    grp = c.groupby(["npi", "tin", "addr_id"], dropna=False)
    ev = grp.agg(sources=("source", lambda s: "|".join(sorted(set(s)))),
                 address_1=("address_1", "first"), address_2=("address_2", lambda s: next((x for x in s if x), "")),
                 city=("city", "first"), state=("state", "first"), zip=("zip", "first"),
                 std_address=("std_address", "first"), geocoded=("geocoded", "max"), lat=("lat", "first"),
                 lon=("lon", "first"), county_fips5=("county_fips5", "first"), tract=("tract", "first"),
                 facility_names=("facility_name", lambda s: " | ".join(sorted({x for x in s if x}))),
                 business_names=("business_names", "first"), nppes_current=("nppes_current", "max")).reset_index()
    # CMS facility registry confirmation (organizations): same building + shared name word, or (ASC) same NPI + ZIP
    ev["registry_match"] = ""
    rpath = s5 / "registry_facilities.parquet"
    if rpath.exists():
        reg = pd.read_parquet(rpath)
        nm = dict(zip(nppes.npi, nppes.name))
        ev["_bkey"] = [raw_key(a, c_, s, z) for a, c_, s, z in zip(ev.address_1, ev.city, ev.state, ev.zip)]
        ev["_names"] = [name_tokens(nm.get(n, "")) | name_tokens(b) for n, b in zip(ev.npi, ev.business_names.fillna(""))]
        ra = reg[reg.reg_address.notna()].copy()
        ra["_bkey"] = [raw_key(a, c_, s, z) for a, c_, s, z in zip(ra.reg_address, ra.reg_city, ra.reg_state, ra.reg_zip)]
        ra["_tok"] = [name_tokens(a) | name_tokens(b or "") for a, b in zip(ra.reg_name.fillna(""), ra.reg_alt_name)]
        by_key = ra.groupby("_bkey").apply(lambda g: list(zip(g.registry, g._tok)), include_groups=False).to_dict()
        asc = reg[reg.reg_npi.notna()]
        asc_key = set(zip(asc.reg_npi.astype(str), asc.reg_zip))
        hits = []
        for k, toks, npi, z in zip(ev._bkey, ev._names, ev.npi, ev.zip):
            lab = {r for r, t in by_key.get(k, []) if toks & t}
            if (str(npi), z) in asc_key:
                lab.add("CMS_ASC")
            hits.append("|".join(sorted(lab)))
        ev["registry_match"] = hits
        ev = ev.drop(columns=["_names"])
    else:
        ev["_bkey"] = [raw_key(a, c_, s, z) for a, c_, s, z in zip(ev.address_1, ev.city, ev.state, ev.zip)]
    # DAC group confirmation: the organization's own Medicare group (exact name) lists this building
    ev["dac_group_match"] = [k in group_keys.get((n, t), ()) for n, t, k in zip(ev.npi, ev.tin, ev._bkey)]
    ev = ev.drop(columns=["_bkey"])
    ev["sources"] = [s + ("|DAC_GROUP" if g else "") + (f"|REGISTRY:{r}" if r else "")
                     for s, g, r in zip(ev.sources, ev.dac_group_match, ev.registry_match)]
    # independent source families: NPPES | CMS enrolment (DAC individual or DAC group) | CMS facility registry
    ev["n_independent_sources"] = (ev.sources.str.contains("NPPES").astype(int)
                                   + ev.sources.str.contains("DAC").astype(int)
                                   + ev.registry_match.ne("").astype(int))
    # TIN corroboration: how many OTHER NPIs on the same TIN report this address in their OWN records
    # (NPPES or individual DAC), so copied group data can never corroborate itself
    own = c[c.source.isin(["NPPES_LOC", "NPPES_SEC", "NPPES_ENDPOINT", "DAC"])].dropna(subset=["npi"])
    per = own.groupby(["tin", "addr_id"]).npi.nunique().rename("npis_on_tin_at_addr")
    ev = ev.merge(per, on=["tin", "addr_id"], how="left")
    ev["npis_on_tin_at_addr"] = ev.npis_on_tin_at_addr.fillna(0).astype(int)
    self_has = ev.sources.str.contains(r"NPPES|(?:^|\|)DAC(?:\||$)", regex=True).astype(int)  # NPI's own record
    ev["tin_corroboration"] = (ev.npis_on_tin_at_addr - (ev.npi.notna().astype(int) & self_has)) >= 1
    ev["dac_name_match"] = [bool(f) and len(name_tokens(f) & name_tokens(b)) / max(1, len(name_tokens(b))) >= 0.5
                            if name_tokens(b) else False for f, b in zip(ev.facility_names, ev.business_names)]
    # the name match is evidence only for INDIVIDUAL DAC rows; DAC_GROUP rows were selected BY name, so counting
    # the same match again would be circular
    ev["dac_name_match"] &= ev.sources.str.contains(r"(?:^|\|)DAC(?:\||$)", regex=True)
    ev["in_state"] = ev.state.eq(cfg["scope"]["state"])
    ev["is_po_box"] = [standardize_address(a).is_po_box for a in ev.address_1]   # never a practice location

    # Tiers (logic D5.5, D5.8). nppes_current is reported but is NOT evidence: on this run's providers,
    # Medicare confirms NPPES addresses updated within 24 months no more often than older ones
    # (data/stage5/c5_audit.txt, check A), so a recent update can't raise an address's tier.
    cond_ver = ev.geocoded & ((ev.n_independent_sources >= 2) |
                              ((ev.n_independent_sources == 1) & (ev.tin_corroboration | ev.dac_name_match)))
    cond_pro = ev.geocoded & (ev.sources.str.contains("DAC") | ev.registry_match.ne(""))
    ev["tier"] = "UNVERIFIED"
    ev.loc[cond_pro & ~ev.is_po_box, "tier"] = "PROBABLE"
    ev.loc[cond_ver & ~ev.is_po_box, "tier"] = "VERIFIED"
    ev["points"] = (2 * ev.sources.str.contains("DAC") + 2 * ev.registry_match.ne("") + ev.sources.str.contains("NPPES_LOC")
                    + ev.sources.str.contains("NPPES_SEC") + ev.sources.str.contains("NPPES_ENDPOINT")
                    + 2 * ev.tin_corroboration + ev.dac_name_match + ev.geocoded + ev.in_state).astype(int)
    ev["tier_rank"] = ev.tier.map({"VERIFIED": 0, "PROBABLE": 1, "UNVERIFIED": 2})
    ev = ev.sort_values(["npi", "tin", "tier_rank", "points"], ascending=[True, True, True, False])
    ev["rank_in_pair"] = ev.groupby(["npi", "tin"], dropna=False).cumcount() + 1
    ev["is_best"] = ev.rank_in_pair.eq(1)
    ev["address"] = ev.std_address.where(ev.geocoded, (ev.address_1.fillna("") + ", " + ev.city.fillna("") + ", "
                                                       + ev.state.fillna("") + ", " + ev.zip.fillna("")))
    ev["address"] = [a if not s2 else f"{a} ({s2})" for a, s2 in zip(ev.address, ev.address_2.fillna(""))]
    ev["evidence"] = ("sources=" + ev.sources + "; tin_corroboration=" + ev.tin_corroboration.astype(str)
                      + "; dac_name_match=" + ev.dac_name_match.astype(str) + "; registry_match=" + ev.registry_match.replace("", "none") + "; nppes_current="
                      + ev.nppes_current.astype(str) + "; geocoded=" + ev.geocoded.astype(str))
    cols = ["npi", "tin", "address", "address_1", "address_2", "city", "state", "zip", "std_address", "addr_id",
            "lat", "lon", "county_fips5", "tract", "tier", "points", "rank_in_pair", "is_best", "sources",
            "n_independent_sources", "tin_corroboration", "npis_on_tin_at_addr", "dac_name_match", "registry_match", "facility_names",
            "nppes_current", "geocoded", "in_state", "is_po_box", "evidence"]
    ev = ev[cols]
    # pairs with no candidate at all are kept with tier NONE
    missing = pairs.merge(ev[["npi", "tin"]].drop_duplicates(), on=["npi", "tin"], how="left", indicator=True)
    missing = missing[missing._merge == "left_only"][["npi", "tin"]].assign(tier="NONE", is_best=True, rank_in_pair=1)
    res = pd.concat([ev, missing], ignore_index=True)
    res.to_parquet(s5 / "npi_tin_address.parquet", index=False)
    save_csv(res, s5 / "npi_tin_address.csv")

    best = res[res.is_best]
    npis = pairs.npi.dropna().unique()
    v_npi = res[res.tier == "VERIFIED"].npi.dropna().unique()
    v_tin = res[res.tier == "VERIFIED"].tin.unique()
    L = [f"C5 address mapping report ({today})", "=" * 70,
         f"(NPI,TIN) pairs: {len(pairs):,} | candidate addresses kept: {len(ev):,} | pairs with no candidate: {len(missing)}",
         f"distinct addresses geocoded: {int(g.geocoded.sum()):,}/{len(g):,} ({100 * g.geocoded.mean():.1f}%)",
         f"best-address tier per pair: {best.tier.value_counts().to_dict()}",
         f"all candidates by tier: {res.tier.value_counts().to_dict()}",
         f"NPIs with >=1 VERIFIED address: {len(v_npi):,}/{len(npis):,} ({100 * len(v_npi) / len(npis):.1f}%)",
         f"TINs with >=1 VERIFIED address: {len(v_tin):,}/{pairs.tin.nunique():,} ({100 * len(v_tin) / pairs.tin.nunique():.1f}%)",
         f"addresses per pair (VERIFIED+PROBABLE): {res[res.tier.isin(['VERIFIED','PROBABLE'])].groupby(['npi','tin'], dropna=False).size().describe()[['mean','50%','max']].round(2).to_dict()}",
         f"distinct VERIFIED addresses per TIN: max {res[res.tier=='VERIFIED'].groupby('tin').addr_id.nunique().max()}",
         f"best address out of state: {int((~best.in_state.fillna(False).astype(bool)).sum())}",
         f"sources on best address: {best.sources.value_counts().head(8).to_dict()}"]
    (s5 / "c5_report.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
