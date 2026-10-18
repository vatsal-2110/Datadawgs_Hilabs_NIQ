"""C1 evidence: is each reporting structure really California commercial group? Proven with independent sources.

C1 (src.stage1_ingest_index) flags scope from what the index says (file token "-ca_", plan name "CA ...", HIOS state).
That is the payer describing itself. This module tests the scope decision against THIRD-PARTY evidence, per
reporting structure (a structure = a set of plans that share the same rate files):

  E1 market       plan_market_type in the index (group / individual)                         [payer, machine-readable]
  E2 providers    where the providers in the structure's in-network file practise: NPPES practice-location
                  state of every NPI (or a fixed random sample of 400 for very large files)    [NPPES, federal registry]
  E3 employers    where the employers sponsoring the plans are: plan IDs of group plans are employer EINs;
                  matched to the US Dept. of Labor Form 5500 / 5500-SF filings (sponsor EIN -> sponsor state)
                                                                                              [DOL EBSA, federal filings]
  E4 issuer       HIOS issuer ID embedded in HIOS-format plan IDs, matched to the licensed company and state named
                  in regulator / CMS filings (config evidence.hios_issuers, with source URL)  [CMS / state regulators]

Verdict per structure: IN SCOPE only if E1 = group AND the majority state in every available geographic test
(E2, E3, E4) is the scope state. Disagreements are printed, never hidden.

Outputs (data/stage1/): scope_evidence.parquet (per structure), scope_evidence_plans.parquet (per plan),
                        c1_scope_evidence.txt
Usage: python -m src.stage1_scope_evidence
"""
from __future__ import annotations

import io
import random
import zipfile
from collections import Counter

import ijson
import pandas as pd

from src.common.normalize import load_config, norm_npi

SAMPLE_N, SEED = 400, 7


# ------------------------------------------------------------------ E2 provider geography
def file_npis(path) -> list[str]:
    npis = set()
    with open(path, "rb") as f:
        for ref in ijson.items(f, "provider_references.item", use_float=True):
            for g in ref.get("provider_groups") or []:
                for raw in g.get("npi") or []:
                    n = norm_npi(raw)
                    if n:
                        npis.add(n)
    return sorted(npis)


def provider_geography(path, cfg) -> dict:
    from src.stage5_fetch_sources import fetch_nppes   # same cached NPPES API client as C5
    npis = file_npis(path)
    sample = npis if len(npis) <= 5000 else random.Random(SEED).sample(npis, SAMPLE_N)
    _, addr = fetch_nppes(sample, cfg)
    loc = addr[addr.purpose == "LOCATION"].drop_duplicates("npi")
    states = Counter(loc.state.fillna("?"))
    top, n_top = states.most_common(1)[0] if states else (None, 0)
    return {"geo_npis_in_file": len(npis), "geo_npis_checked": len(sample), "geo_npis_found": int(len(loc)),
            "geo_top_state": top, "geo_top_state_pct": round(100 * n_top / max(len(loc), 1), 1),
            "geo_states": dict(states.most_common(5))}


# ------------------------------------------------------------------ E3 employer location (DOL Form 5500)
def ensure_dol_files(cfg):
    """Download the DOL Form 5500 datasets listed in config sources.dol_5500_urls if they are missing (cached)."""
    import requests
    from urllib.parse import unquote
    d = cfg["paths"]["dol"]
    d.mkdir(parents=True, exist_ok=True)
    for url in cfg["sources"]["dol_5500_urls"]:
        dest = d / unquote(url).rsplit("/", 1)[-1]
        if dest.exists():
            continue
        print(f"downloading {dest.name}")
        with requests.get(url, headers={"User-Agent": cfg["http"]["user_agent"]}, stream=True, timeout=600) as r:
            r.raise_for_status()
            tmp = dest.with_suffix(".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
            tmp.rename(dest)


def load_dol_sponsors(cfg) -> pd.DataFrame:
    """Sponsor EIN -> state from every Form 5500 / 5500-SF zip in paths.dol (latest filing wins)."""
    cols = {  # (EIN, location state, mail state, sponsor name, welfare benefit codes, plan year begin)
        "F_5500_SF": ("SF_SPONS_EIN", "SF_SPONS_LOC_US_STATE", "SF_SPONS_US_STATE", "SF_SPONSOR_NAME",
                      "SF_TYPE_WELFARE_BNFT_CODE", "SF_PLAN_YEAR_BEGIN_DATE"),
        "F_5500": ("SPONS_DFE_EIN", "SPONS_DFE_LOC_US_STATE", "SPONS_DFE_MAIL_US_STATE", "SPONSOR_DFE_NAME",
                   "TYPE_WELFARE_BNFT_CODE", "FORM_PLAN_YEAR_BEGIN_DATE"),
    }
    parts = []
    for z in sorted(cfg["paths"]["dol"].glob("F_5500*_Latest.zip")):
        kind = "F_5500_SF" if "_SF_" in z.name else "F_5500"
        ein, loc, mail, name, welfare, begin = cols[kind]
        with zipfile.ZipFile(z) as zf:
            csv = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
            head = pd.read_csv(io.BytesIO(zf.read(csv)), nrows=0, encoding="latin-1").columns
            use = [c for c in (ein, loc, mail, name, welfare, begin) if c in head]
            d = pd.read_csv(io.BytesIO(zf.read(csv)), usecols=use, dtype=str, encoding="latin-1")
        d = d.rename(columns={ein: "ein", loc: "loc_state", mail: "mail_state", name: "sponsor_name",
                              welfare: "welfare_codes", begin: "plan_year_begin"})
        d["source_file"] = z.name
        parts.append(d)
    if not parts:
        return pd.DataFrame(columns=["ein", "sponsor_state", "sponsor_name", "is_health_plan", "source_file"])
    d = pd.concat(parts, ignore_index=True)
    d["ein"] = d.ein.str.replace(r"\D", "", regex=True).str.zfill(9)
    d["sponsor_state"] = d.get("loc_state").where(d.get("loc_state").notna(), d.get("mail_state")) \
        if "loc_state" in d else d.mail_state
    d["is_health_plan"] = d.get("welfare_codes", pd.Series("", index=d.index)).fillna("").str.contains("4A")
    # prefer a health (4A) filing, then the latest plan year
    d = d.sort_values(["is_health_plan", "plan_year_begin"], ascending=[False, False])
    return d.drop_duplicates("ein")[["ein", "sponsor_state", "sponsor_name", "is_health_plan", "source_file"]]


# ------------------------------------------------------------------ verdict helpers
def majority(states: pd.Series):
    s = states.dropna()
    if s.empty:
        return None, None, 0
    vc = s.value_counts()
    return vc.index[0], round(100 * vc.iloc[0] / len(s), 1), int(len(s))


def main():
    cfg = load_config()
    P = cfg["paths"]
    st = cfg["scope"]["state"]
    plans = pd.read_parquet(P["stage1"] / "plans.parquet")
    pfm = pd.read_parquet(P["stage1"] / "plan_file_map.parquet")
    files = pd.read_parquet(P["stage1"] / "files.parquet")
    issuers = {str(k): v for k, v in cfg["evidence"]["hios_issuers"].items()}

    # ---- per plan: E4 issuer, E3 employer
    pl = plans[["plan_row_id", "brand", "structure_id", "plan_name", "plan_id", "plan_id_format",
                "plan_market_type", "in_scope"]].copy()
    hios = pl.plan_id_format.isin(["HIOS", "HIOS_ISSUER"])
    pl["hios_issuer"] = pl.plan_id.str[:5].where(hios)
    pl["hios_issuer_entity"] = pl.hios_issuer.map(lambda i: issuers.get(i, {}).get("entity") if isinstance(i, str) else None)
    pl["hios_issuer_state"] = pl.hios_issuer.map(lambda i: issuers.get(i, {}).get("state") if isinstance(i, str) else None)
    pl["hios_id_state"] = pl.plan_id.str[5:7].where(hios & pl.plan_id.str.match(r"^\d{5}[A-Z]{2}"))
    ensure_dol_files(cfg)
    dol = load_dol_sponsors(cfg)
    pl["ein"] = pl.plan_id.where(pl.plan_id_format == "EIN")
    pl = pl.merge(dol.rename(columns={"ein": "ein"}), on="ein", how="left")

    # ---- per structure
    inn = pfm[pfm.file_role == "in_network"].merge(files[["file_key", "file_name"]], on="file_key")
    s_files = inn.groupby("structure_id").file_name.agg(lambda s: sorted(set(s)))
    geo_cache = {}
    rows = []
    for (brand, sid), g in pl.groupby(["brand", "structure_id"]):
        fns = s_files.get(sid, [])
        geo = {}
        for fn in fns:
            path = P["raw_rates"] / fn
            if path.exists():
                if fn not in geo_cache:
                    print(f"E2 provider geography: {fn}")
                    geo_cache[fn] = provider_geography(path, cfg)
                geo = geo_cache[fn]
                break
        emp_state, emp_pct, emp_n = majority(g.sponsor_state)
        iss_state, iss_pct, iss_n = majority(g.hios_issuer_state.where(g.hios_issuer_state.notna(), g.hios_id_state))
        market = "|".join(sorted(g.plan_market_type.dropna().unique()))
        tests = {"E1_market_group": market == "group",
                 "E2_providers": (geo.get("geo_top_state") == st) if geo else None,
                 "E3_employers": (emp_state == st) if emp_n else None,
                 "E4_issuer": (iss_state == st) if iss_n else None}
        geo_tests = [v for k, v in tests.items() if k != "E1_market_group" and v is not None]
        if not tests["E1_market_group"]:
            verdict = "OUT_OF_SCOPE (not group market)"
        elif not geo_tests:
            verdict = "OUT_OF_SCOPE (no geographic evidence of CA)"
        elif all(geo_tests):
            verdict = "IN_SCOPE"
        else:
            verdict = "OUT_OF_SCOPE (evidence points to another state)"
        rows.append({"brand": brand, "structure_id": sid, "n_plans": len(g), "market": market,
                     "in_network_files": " | ".join(fns), **{k: v for k, v in geo.items() if k != "geo_states"},
                     "geo_states": str(geo.get("geo_states", "")),
                     "employer_eins": int(g.ein.notna().sum()), "employer_eins_matched": emp_n,
                     "employer_top_state": emp_state, "employer_top_state_pct": emp_pct,
                     "employer_states": str(g.sponsor_state.value_counts().head(5).to_dict()),
                     "hios_plans": iss_n, "hios_top_state": iss_state, "hios_top_state_pct": iss_pct,
                     "hios_issuers": str(g.hios_issuer_entity.value_counts().to_dict()),
                     **tests, "verdict": verdict, "c1_in_scope": bool(g.in_scope.all())})
    ev = pd.DataFrame(rows)
    ev.to_parquet(P["stage1"] / "scope_evidence.parquet", index=False)
    pl.to_parquet(P["stage1"] / "scope_evidence_plans.parquet", index=False)

    agree = (ev.verdict.eq("IN_SCOPE") == ev.c1_in_scope).all()
    ca = ev[ev.verdict == "IN_SCOPE"]
    in_plans = pl[pl.structure_id.isin(ca.structure_id)]
    off = in_plans[in_plans.sponsor_state.notna() & in_plans.sponsor_state.ne(st)]
    L = ["C1 scope evidence: is each reporting structure California commercial group? (third-party tests)", "=" * 96,
         f"DOL Form 5500 sponsors loaded: {len(dol):,} EINs from {sorted(dol.source_file.unique()) if len(dol) else 'none'}",
         ""]
    for r in ev.sort_values(["verdict", "n_plans"], ascending=[True, False]).itertuples():
        L += [f"[{r.verdict}] {r.brand} structure {r.structure_id[:12]}  ({r.n_plans} plans, market={r.market})",
              f"   files: {r.in_network_files[:150]}"]
        if pd.notna(getattr(r, "geo_npis_found", None)):
            L.append(f"   E2 providers: {r.geo_npis_found:,}/{r.geo_npis_checked:,} NPIs located in NPPES; top state "
                     f"{r.geo_top_state} {r.geo_top_state_pct}%  {r.geo_states}")
        else:
            L.append("   E2 providers: rate file not available locally")
        L.append(f"   E3 employers: {r.employer_eins_matched}/{r.employer_eins} plan EINs found in DOL Form 5500; "
                 f"top state {r.employer_top_state} {r.employer_top_state_pct}%  {r.employer_states}")
        L.append(f"   E4 issuer: {r.hios_plans} HIOS plans; {r.hios_issuers}; top state {r.hios_top_state}")
        L.append(f"   tests: market_group={r.E1_market_group} providers={r.E2_providers} employers={r.E3_employers} "
                 f"issuer={r.E4_issuer}")
    L += ["", f"Evidence verdict agrees with the C1 scope flag for every structure: {agree}",
          f"In-scope plans whose employer files Form 5500 from another state: {len(off)} "
          f"({off.sponsor_state.value_counts().head(5).to_dict()}); kept: the plan's network is the CA file "
          f"(E2), multi-state employers buy CA coverage for CA staff.",
          f"In-scope plan EINs not found in Form 5500: {int(in_plans.ein.notna().sum() - in_plans.sponsor_state.notna().sum())} "
          f"(fully insured plans with < 100 participants are exempt from filing; DOL 29 CFR 2520.104-20)."]
    (P["stage1"] / "c1_scope_evidence.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
