"""C3 scorecard: grade every PTD file we use with metrics that test usability against outside references.

Three file types, each judged on its own job (thresholds: config grading.scorecard; field rules: the official
CMS price-transparency schemas). Every metric is a 0-100 score (higher = better); overall = weighted mean,
then the Trust Gate caps it.

IN-NETWORK rate files (in-scope file + config grading.compare_files peers). Unit: price record.
  Contract-Ready Rate Yield   30  % of HiLabs-code services (provider group x service) that are usable: ONE distinct
                                  price, plausible (within the Medicare band when benchmarkable, and not a robust peer
                                  outlier), and the provider group lists >= 1 valid NPI
  Medicare Reality Check      20  % of benchmarkable professional HiLabs prices within the Medicare band
                                  (2026 PFS, CA median geographic factors); coverage reported
  Specialty Fee-Schedule Depth 20 per NPI with a HiLabs specialty: share of that specialty's top-N HiLabs codes it is
                                  priced on, averaged (all NPIs up to full_npi_max, else a fixed sample)
  Single-Price Certainty      15  % of (provider group, service) combinations, all codes, with exactly one price
  Field Coherence              5  % of prices whose code type, billing class, place of service, modifier, arrangement
                                  and rate type follow the CMS schema and agree with each other
  Filler-Free Score            5  100 - % of checked fields holding filler: empty network_name (schema-required),
                                  NPI 0 / failing the check digit, TIN not 9 digits or one repeated digit
  Live Provider Rate           5  % of NPIs found in NPPES and updated within live_provider_years

ALLOWED-AMOUNTS files (in-scope + config grading.compare_allowed_files). Unit: payment record
(item x TIN x payment x provider). Missing codes are legal (< 20 claims may be omitted), so no coverage metric.
  Usable Payment Yield        30  % of records that are schema-complete, coherent, allowed <= billed, within the
                                  Medicare band when benchmarkable, with >= 1 valid NPI
  Paid-to-Billed Realism      25  % of records with allowed / billed inside paid_share_band
  Medicare Reality Check      15  % of benchmarkable professional payments within the Medicare band
  In-Network Anchoring        10  % of records whose allowed amount is within anchor_band x the same payer's
                                  in-network median for the same code and billing class (codes in both files)
  Field Coherence             10  code type allowed, billing class allowed, revenue codes institutional,
                                  professional rows carry a place of service, TIN type ein/npi
  Schema Completeness          5  every field the CMS allowed-amounts schema requires
  Filler-Free Score            5  100 - % filler fields: NPI 0 / bad check digit, placeholder TIN, token billed
                                  charge (<= token_billed_max)

INDEX files (all used in C1). Unit: plan row.
  Price Reachability          35  % of plan rows whose in-network file link(s) are actually served (HTTP 200, live)
  Plan ID Integrity           25  % plan rows whose plan_id follows the CMS rule for its plan_id_type
  Plan-to-File Clarity        15  % of distinct plans (brand + normalized name + id + market) mapped to exactly one
                                  set of in-network files
  Filler-Free Score           10  100 - % rows with placeholder ids, exact duplicates, or from a byte-identical index
  Schema Completeness         10  every field the CMS table-of-contents schema requires
  Freshness                    5  100 if updated <= 31 days ago, linear to 0 at 365 days

Trust Gate (all types): file older than gate_max_age_days, or fewer valid NPIs than gate_min_npi_valid_pct
(rate and allowed files) -> overall capped at gate_cap.

Outputs: data/stage3/scorecard_{in_network,allowed,index}.csv (+ .parquet), data/stage3/scorecard_report.txt
Usage  : python -m src.stage3_scorecard
"""
from __future__ import annotations

import json
import math
import random
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import date
from pathlib import Path
from statistics import median

import ijson
import pandas as pd
import requests

from src.common.normalize import load_config, norm_npi
from src.stage1_ingest_index import download
from src.stage3_grade_files import npi_check_digit_ok
from src.stage4_medicare_benchmark import FACILITY_POS, load_pfs

W_IN = {"Contract-Ready Rate Yield": 30, "Medicare Reality Check": 20, "Specialty Fee-Schedule Depth": 20,
        "Single-Price Certainty": 15, "Field Coherence": 5, "Filler-Free Score": 5, "Live Provider Rate": 5}
W_AA = {"Usable Payment Yield": 30, "Paid-to-Billed Realism": 25, "Medicare Reality Check": 15,
        "In-Network Anchoring": 10, "Field Coherence": 10, "Schema Completeness": 5, "Filler-Free Score": 5}
W_IX = {"Price Reachability": 35, "Plan ID Integrity": 25, "Plan-to-File Clarity": 15, "Filler-Free Score": 10,
        "Schema Completeness": 10, "Freshness": 5}

CODE_TYPES = {"CPT", "NDC", "HCPCS", "RC", "ICD", "MS-DRG", "R-DRG", "S-DRG", "APS-DRG", "AP-DRG", "APR-DRG", "APC",
              "LOCAL", "EAPG", "HIPPS", "CDT", "CSTM-ALL"}                       # CMS schema list
NEG_TYPES = {"negotiated", "derived", "fee schedule", "percentage", "per diem"}
DOLLAR = {"negotiated", "derived", "fee schedule"}
ARRANGEMENTS = {"ffs", "bundle", "capitation"}


# ---------------------------------------------------------------- shared helpers
def id_valid(pid: str, id_type: str, fmt: str) -> bool:
    """CMS table-of-contents schema: ein -> the plan sponsor's EIN; hios -> the 10-digit HIOS id, or the 5-digit one
    when the 10-digit id is not available. Placeholders (999999999) are never valid."""
    if fmt in ("PLACEHOLDER", "MISSING", "OTHER"):
        return False
    return (id_type == "ein" and fmt == "EIN") or (id_type == "hios" and fmt in ("HIOS", "HIOS_ISSUER"))


def _tin_ok(v) -> bool:
    d = re.sub(r"\D", "", str(v or ""))
    return len(d) == 9 and len(set(d)) > 1


def _freshness(age):
    if age is None:
        return 0.0
    return 100.0 if age <= 31 else max(0.0, 100 * (365 - age) / (365 - 31))


def _header_date(path: Path):
    with open(path, "rb") as f:
        m = re.search(r'"last_updated_on"\s*:\s*"([^"]*)"', f.read(8192).decode("utf-8", "ignore"))
    return m.group(1) if m else None


def _overall(scores: dict, weights: dict, gate_fail: bool, cap: float):
    raw = sum(scores[k] * w for k, w in weights.items()) / sum(weights.values())
    return round(min(raw, cap) if gate_fail else raw, 1), round(raw, 1)


class Medicare:
    """2026 PFS amount for a professional line at the CA median geographic factors (no provider locality here,
    so every graded file is benchmarked the same way)."""

    def __init__(self, pfs_zip):
        pfs, gp, _ = load_pfs(pfs_zip)
        self.g = gp[["g_work", "g_pe", "g_mp"]].median()
        self.p = {(r.code, r.mod): r for r in pfs.itertuples()}

    def amount(self, code, mods, pos):
        mod = "26" if "26" in mods else ("TC" if "TC" in mods else "")
        r = self.p.get((code, mod))
        if r is None:
            return None
        fac = bool(pos) and set(pos) <= FACILITY_POS
        pe = r.pe_f if fac else r.pe_nf
        amt = (r.work * self.g.g_work + pe * self.g.g_pe + r.mp * self.g.g_mp) * r.cf
        return amt if amt and amt > 0 and not math.isnan(amt) else None


# ---------------------------------------------------------------- in-network
def grade_in_network(args):
    (path, file_name, role, run_date, codes, spec_top, xwalk, fac_map, nppes_rows, target_npis, pfs_zip, sc) = args
    path, med = Path(path), Medicare(pfs_zip)
    lo, hi = sc["medicare_band_pct"]
    updated = _header_date(path)
    age = (date.fromisoformat(run_date) - date.fromisoformat(updated)).days if updated else None

    # pass 1: provider references
    ref_npis, ref_valid = {}, {}
    fields = filler = npi_tot = npi_ok = 0
    with open(path, "rb") as f:
        for r in ijson.items(f, "provider_references.item", use_float=True):
            rid = r.get("provider_group_id")
            fields += 1
            if not [x for x in (r.get("network_name") or []) if x]:
                filler += 1
            npis, valid = set(), False
            for g in r.get("provider_groups") or []:
                fields += 1
                if not _tin_ok((g.get("tin") or {}).get("value")):
                    filler += 1
                for raw in g.get("npi") or []:
                    fields += 1
                    npi_tot += 1
                    s = norm_npi(raw)
                    if s and npi_check_digit_ok(s):
                        npi_ok += 1
                        valid = True
                        npis.add(s)
                    else:
                        filler += 1
            ref_npis[rid], ref_valid[rid] = npis, valid

    # pass 2: prices
    svc_rates = defaultdict(set)              # (ref, service) -> distinct rates, all codes
    hil_keys = defaultdict(set)               # HiLabs services only
    hil_meta = {}                             # key -> (code, class, mods, pos)
    priced = defaultdict(set)                 # target NPI -> HiLabs codes priced
    n_price = coherent = 0
    in_med = defaultdict(list)                # (code_type, code, class) -> rates (for In-Network Anchoring)
    with open(path, "rb") as f:
        for it in ijson.items(f, "in_network.item", use_float=True):
            ctype, code = it.get("billing_code_type"), str(it.get("billing_code"))
            arr, name = it.get("negotiation_arrangement"), it.get("name")
            for b in it.get("negotiated_rates") or []:
                refs = b.get("provider_references") or []
                for p in b.get("negotiated_prices") or []:
                    n_price += 1
                    nt, bc, rate = p.get("negotiated_type"), p.get("billing_class"), p.get("negotiated_rate")
                    mods = tuple(sorted(p.get("billing_code_modifier") or []))
                    pos = tuple(sorted(p.get("service_code") or []))
                    add = p.get("additional_information")
                    ok = (ctype in CODE_TYPES and bc in ("professional", "institutional", "both") and arr in ARRANGEMENTS
                          and nt in NEG_TYPES and (ctype != "RC" or bc in ("institutional", "both"))
                          and (bc != "professional" or bool(pos))
                          and (not ({"26", "TC"} & set(mods)) or bc in ("professional", "both")))
                    coherent += ok
                    if nt not in DOLLAR or rate is None or float(rate) <= 0:
                        continue
                    rate = float(rate)
                    in_med[(ctype, code, bc)].append(rate)
                    svc = (ctype, code, name, bc, mods, pos, add, arr)
                    for r in refs:
                        svc_rates[(r, svc)].add(rate)
                    # HiLabs service: the item's own code, or a CPT/HCPCS carried by a revenue-code price
                    hc = code if code in codes else (str(add) if ctype == "RC" and add and str(add) in codes else None)
                    if hc:
                        for r in refs:
                            k = (r, svc)
                            hil_keys[k].add(rate)
                            hil_meta[k] = (hc, bc, mods, pos)
                            for n in ref_npis.get(r, ()):
                                if n in target_npis:
                                    priced[n].add(hc)

    # Medicare + peer plausibility on HiLabs services
    groups = defaultdict(list)
    for k, rs in hil_keys.items():
        hc, bc, mods, _ = hil_meta[k]
        for x in rs:
            groups[(hc, bc, mods)].append(math.log(x))
    gstat = {}
    for g, v in groups.items():
        if len(v) >= sc["peer_min_group"]:
            m = median(v)
            mad = median(abs(x - m) for x in v)
            gstat[g] = (m, mad)

    def peer_ok(g, x):
        st = gstat.get(g)
        if not st or st[1] == 0:
            return True
        return abs(0.6745 * (math.log(x) - st[0]) / st[1]) <= sc["peer_z_max"]

    bench_n = bench_ok = usable = 0
    for k, rs in hil_keys.items():
        hc, bc, mods, pos = hil_meta[k]
        m_amt = med.amount(hc, mods, pos) if bc == "professional" else None
        plaus = True
        for x in rs:
            if m_amt:
                bench_n += 1
                inb = lo <= 100 * x / m_amt <= hi
                bench_ok += inb
                plaus &= inb
            plaus &= peer_ok((hc, bc, mods), x)
        usable += (len(rs) == 1 and plaus and ref_valid.get(k[0], False))

    # specialty depth + live providers over target NPIs
    depth, live, n_target = [], 0, 0
    yrs = sc["live_provider_years"] * 365.25
    for row in nppes_rows:
        if row["npi"] not in target_npis:
            continue
        n_target += 1
        if row["found"] and row.get("last_updated"):
            if (date.fromisoformat(run_date) - date.fromisoformat(row["last_updated"])).days <= yrs:
                live += 1
        tx = (row.get("taxonomy_codes") or "").split("|") if row["found"] else []
        prim = row.get("primary_taxonomy")
        spec = xwalk.get(prim) or next((xwalk[c] for c in tx if c in xwalk), None) or \
            next((fac_map[c] for c in tx if c in fac_map), None)
        if spec and spec_top.get(spec):
            top = spec_top[spec]
            depth.append(len(priced.get(row["npi"], set()) & top) / len(top))

    n_keys = len(svc_rates)
    single = sum(1 for v in svc_rates.values() if len(v) == 1)
    npi_valid_pct = 100 * npi_ok / max(npi_tot, 1)
    s = {"Contract-Ready Rate Yield": 100 * usable / max(len(hil_keys), 1),
         "Medicare Reality Check": 100 * bench_ok / max(bench_n, 1) if bench_n else 0.0,
         "Specialty Fee-Schedule Depth": 100 * sum(depth) / len(depth) if depth else 0.0,
         "Single-Price Certainty": 100 * single / max(n_keys, 1),
         "Field Coherence": 100 * coherent / max(n_price, 1),
         "Filler-Free Score": 100 - 100 * filler / max(fields, 1),
         "Live Provider Rate": 100 * live / max(n_target, 1)}
    s = {k: round(v, 2) for k, v in s.items()}
    gate = (age is None or age > sc["gate_max_age_days"]) or npi_valid_pct < sc["gate_min_npi_valid_pct"]
    overall, raw_overall = _overall(s, W_IN, gate, sc["gate_cap"])
    medians = {k: median(v) for k, v in in_med.items()}
    info = {"file_name": file_name, "role": role, "last_updated_on": updated, "age_days": age,
            "price_records": n_price, "hilabs_services": len(hil_keys), "usable_hilabs_services": usable,
            "medicare_benchmarked_prices": bench_n, "medicare_coverage_pct": round(100 * bench_n /
            max(sum(len(v) for v in hil_keys.values()), 1), 1),
            "service_combinations": n_keys, "single_price_combinations": single,
            "npis_scored": n_target, "npis_with_specialty": len(depth), "npi_valid_pct": round(npi_valid_pct, 3),
            "filler_fields": filler, "checked_fields": fields, "trust_gate_failed": gate,
            "overall_before_gate": raw_overall, "overall_score": overall}
    return {**info, **s}, medians


# ---------------------------------------------------------------- allowed amounts
def grade_allowed(args):
    path, file_name, role, run_date, in_medians, pfs_zip, sc = args
    path, med = Path(path), Medicare(pfs_zip)
    lo, hi = sc["medicare_band_pct"]
    plo, phi = sc["paid_share_band"]
    alo, ahi = sc["anchor_band"]
    updated = _header_date(path)
    age = (date.fromisoformat(run_date) - date.fromisoformat(updated)).days if updated else None
    rec = complete = coherent = realistic = both = bench_n = bench_ok = anch_n = anch_ok = usable = 0
    fields = filler = npi_tot = npi_ok = 0
    with open(path, "rb") as f:
        for it in ijson.items(f, "out_of_network.item", use_float=True):
            ctype, code = it.get("billing_code_type"), str(it.get("billing_code") or "")
            item_ok = bool(it.get("name")) and bool(ctype) and bool(code)
            for aa in it.get("allowed_amounts") or []:
                t = aa.get("tin") or {}
                bc = aa.get("billing_class")
                pos = tuple(sorted(aa.get("service_code") or []))
                fields += 1
                tin_ok = _tin_ok(t.get("value"))
                filler += not tin_ok
                coh = (ctype in CODE_TYPES and bc in ("professional", "institutional")
                       and (ctype != "RC" or bc == "institutional") and (bc != "professional" or bool(pos))
                       and t.get("type") in ("ein", "npi"))
                for p in aa.get("payments") or []:
                    amt = p.get("allowed_amount")
                    amt = float(amt) if amt is not None else None
                    mods = tuple(sorted(p.get("billing_code_modifier") or []))
                    for pr in p.get("providers") or []:
                        rec += 1
                        billed = pr.get("billed_charge")
                        billed = float(billed) if billed is not None else None
                        fields += 1
                        filler += billed is not None and billed <= sc["token_billed_max"]
                        nl = pr.get("npi") or []
                        valid_npi = False
                        for raw in nl:
                            fields += 1
                            npi_tot += 1
                            s_ = norm_npi(raw)
                            if s_ and npi_check_digit_ok(s_):
                                npi_ok += 1
                                valid_npi = True
                            else:
                                filler += 1
                        comp = (item_ok and t.get("type") and bool(str(t.get("value") or "")) and bc is not None
                                and (bc != "professional" or bool(pos)) and amt is not None and billed is not None
                                and len(nl) > 0)
                        complete += comp
                        coherent += coh
                        le = amt is not None and billed is not None and amt > 0 and billed > 0 and amt <= billed
                        if amt and billed and amt > 0 and billed > 0:
                            both += 1
                            realistic += plo <= amt / billed <= phi
                        m_amt = med.amount(code, mods, pos) if bc == "professional" and ctype in ("CPT", "HCPCS") else None
                        inband = True
                        if m_amt and amt:
                            bench_n += 1
                            inband = lo <= 100 * amt / m_amt <= hi
                            bench_ok += inband
                        anchor = in_medians.get((ctype, code, bc))
                        if anchor and amt:
                            anch_n += 1
                            anch_ok += alo <= amt / anchor <= ahi
                        usable += bool(comp and coh and le and inband and valid_npi)
    npi_valid_pct = 100 * npi_ok / max(npi_tot, 1)
    s = {"Usable Payment Yield": 100 * usable / max(rec, 1),
         "Paid-to-Billed Realism": 100 * realistic / max(both, 1),
         "Medicare Reality Check": 100 * bench_ok / max(bench_n, 1) if bench_n else 0.0,
         "In-Network Anchoring": 100 * anch_ok / max(anch_n, 1) if anch_n else 0.0,
         "Field Coherence": 100 * coherent / max(rec, 1),
         "Schema Completeness": 100 * complete / max(rec, 1),
         "Filler-Free Score": 100 - 100 * filler / max(fields, 1)}
    s = {k: round(v, 2) for k, v in s.items()}
    gate = (age is None or age > sc["gate_max_age_days"]) or npi_valid_pct < sc["gate_min_npi_valid_pct"]
    overall, raw_overall = _overall(s, W_AA, gate, sc["gate_cap"])
    info = {"file_name": file_name, "role": role, "last_updated_on": updated, "age_days": age, "payment_records": rec,
            "medicare_benchmarked": bench_n, "anchored_records": anch_n, "npi_valid_pct": round(npi_valid_pct, 3),
            "trust_gate_failed": gate, "overall_before_gate": raw_overall, "overall_score": overall}
    return {**info, **s}


# ---------------------------------------------------------------- index files
def check_links(urls, cfg):
    cache_f = cfg["paths"]["cache"] / "url_check.json"
    cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}
    todo = [u for u in urls if u not in cache]

    def head(u):
        for i in range(3):
            try:
                r = requests.head(u, headers={"User-Agent": cfg["http"]["user_agent"], "Accept-Encoding": "identity"},
                                  timeout=60, allow_redirects=True)
                return u, r.status_code
            except requests.RequestException:
                time.sleep(2 ** i)
        return u, -1

    with ThreadPoolExecutor(cfg["http_pool"]["workers"]) as ex:
        for u, c in ex.map(head, todo):
            cache[u] = c
    cache_f.parent.mkdir(parents=True, exist_ok=True)
    cache_f.write_text(json.dumps(cache, indent=0), encoding="utf-8")
    return cache


def grade_index(cfg, run_date):
    s1 = cfg["paths"]["stage1"]
    plans = pd.read_parquet(s1 / "plans.parquet")
    idx = pd.read_parquet(s1 / "index_files.parquet")
    pfm = pd.read_parquet(s1 / "plan_file_map.parquet")
    files = pd.read_parquet(s1 / "files.parquet")[["file_key", "example_url"]]
    inn = pfm[pfm.file_role == "in_network"].merge(files, on="file_key")
    status = check_links(sorted(inn.example_url.unique()), cfg)
    inn["ok"] = inn.example_url.map(lambda u: status.get(u) == 200)
    reach = inn.groupby("plan_row_id").ok.all()
    filesets = inn.groupby("plan_row_id").file_key.agg(lambda s: tuple(sorted(set(s))))
    rows = []
    for r in idx.itertuples():
        p = plans[plans.index_name == r.index_name].copy()
        n = max(len(p), 1)
        reachable = p.plan_row_id.map(reach).fillna(False).astype(bool)
        valid = [id_valid(a, b, c) for a, b, c in zip(p.plan_id, p.plan_id_type, p.plan_id_format)]
        p["fs"] = p.plan_row_id.map(filesets)
        clear = p.groupby("plan_key").fs.agg(lambda s: s.notna().all() and s.nunique() == 1)
        dup = p.duplicated(["structure_id", "plan_name", "plan_id", "plan_market_type"])
        fill = (p.plan_id_format.eq("PLACEHOLDER") | dup | bool(r.identical_to))
        complete = (p.plan_name.str.len().gt(0) & p.plan_id.str.len().gt(0) & p.plan_id_type.isin(["ein", "hios"])
                    & p.plan_market_type.isin(["group", "individual"]) & p.issuer.fillna("").str.len().gt(0)
                    & (p.plan_id_type.ne("ein") | p.sponsor.fillna("").str.len().gt(0)))
        age = (date.fromisoformat(run_date) - date.fromisoformat(r.last_updated_on)).days if r.last_updated_on else None
        s = {"Price Reachability": 100 * reachable.mean() if len(p) else 0.0,
             "Plan ID Integrity": 100 * sum(valid) / n,
             "Plan-to-File Clarity": 100 * clear.mean() if len(clear) else 0.0,
             "Filler-Free Score": 100 - 100 * fill.mean() if len(p) else 0.0,
             "Schema Completeness": 100 * complete.mean() if len(p) else 0.0,
             "Freshness": _freshness(age)}
        s = {k: round(v, 2) for k, v in s.items()}
        gate = age is None or age > cfg["grading"]["scorecard"]["gate_max_age_days"]
        overall, raw_overall = _overall(s, W_IX, gate, cfg["grading"]["scorecard"]["gate_cap"])
        rows.append({"index_name": r.index_name, "brand": r.brand, "last_updated_on": r.last_updated_on,
                     "age_days": age, "plan_rows": len(p), "unreachable_plan_rows": int((~reachable).sum()),
                     "invalid_plan_ids": int(n - sum(valid)) if len(p) else 0, "ambiguous_plans": int((~clear).sum()),
                     "filler_rows": int(fill.sum()), "identical_to": r.identical_to or "",
                     "trust_gate_failed": gate, "overall_before_gate": raw_overall, "overall_score": overall, **s})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- main
def main():
    cfg = load_config()
    P, sc = cfg["paths"], cfg["grading"]["scorecard"]
    s3 = P["stage3"]
    s3.mkdir(parents=True, exist_ok=True)
    run_date = date.today().isoformat()
    pfs_zip = P["cms_pfs"] / "rvu26d.zip"
    if not pfs_zip.exists():
        pfs_zip.parent.mkdir(parents=True, exist_ok=True)
        download(cfg["sources"]["cms_pfs_rvu_zip"], pfs_zip, cfg)

    x = pd.read_excel(P["hilabs_xlsx"], sheet_name=None, dtype=str)
    sp = x["Spec-Proc Weights"].assign(w=lambda d: d.SPEC_PROC_WEIGHT.astype(float))
    codes = set(sp.PROC_CD)
    spec_top = {s: set(g.sort_values("w", ascending=False).PROC_CD.head(sc["top_codes_per_specialty"]))
                for s, g in sp.groupby("SPEC_CD")}
    xwalk = dict(zip(x["Taxonomy-Spec Xwalk"]["Taxonomy Code"], x["Taxonomy-Spec Xwalk"].SPEC_CD))
    fac_map = {k: str(v) for k, v in cfg["specialty"]["facility_taxonomy_map"].items()}

    files = pd.read_parquet(P["stage1"] / "files.parquet")

    def pick(role_col, compare_key):
        f = files[files.file_role == role_col]
        sel = f[f.in_scope].assign(role="USED (in scope)")
        for pat in cfg["grading"].get(compare_key, []):
            sel = pd.concat([sel, f[f.file_name.str.contains(pat, regex=False)].assign(role="comparison")])
        out = []
        for r in sel.itertuples():
            dest = P["raw_rates"] / r.file_name
            if not dest.exists():
                download(r.example_url, dest, cfg)
            out.append((dest, r.file_name, r.role))
        return out

    # NPIs per in-network file (pass over provider_references only) -> all NPIs or a fixed sample
    from src.stage5_fetch_sources import fetch_nppes
    in_jobs = []
    for dest, fname, role in pick("in_network", "compare_files"):
        npis = set()
        with open(dest, "rb") as f:
            for r in ijson.items(f, "provider_references.item"):
                for g in r.get("provider_groups") or []:
                    for raw in g.get("npi") or []:
                        s_ = norm_npi(raw)
                        if s_:
                            npis.add(s_)
        target = npis if len(npis) <= sc["full_npi_max"] else \
            set(random.Random(7).sample(sorted(npis), sc["npi_sample"]))
        nd, _ = fetch_nppes(sorted(target), cfg)
        rows = nd.where(pd.notna(nd), None).to_dict("records")
        in_jobs.append((str(dest), fname, role, run_date, codes, spec_top, xwalk, fac_map, rows, target, str(pfs_zip), sc))

    print(f"grading {len(in_jobs)} in-network files in parallel ...")
    with ProcessPoolExecutor(max_workers=min(len(in_jobs), 4)) as ex:
        in_res = list(ex.map(grade_in_network, in_jobs))
    inn = pd.DataFrame([r for r, _ in in_res])
    medians_by_file = {r["file_name"]: m for r, m in in_res}

    aa_jobs = []
    for dest, fname, role in pick("allowed_amount", "compare_allowed_files"):
        twin = fname.replace("_allowed-amounts", "_in-network")
        aa_jobs.append((str(dest), fname, role, run_date, medians_by_file.get(twin, {}), str(pfs_zip), sc))
    with ProcessPoolExecutor(max_workers=max(1, min(len(aa_jobs), 4))) as ex:
        aa = pd.DataFrame(list(ex.map(grade_allowed, aa_jobs)))
    ix = grade_index(cfg, run_date)

    for name, df in (("in_network", inn), ("allowed", aa), ("index", ix)):
        df.to_parquet(s3 / f"scorecard_{name}.parquet", index=False)
        df.to_csv(s3 / f"scorecard_{name}.csv", index=False, encoding="utf-8")

    def block(title, df, keycol, weights):
        cols = [keycol] + (["role"] if "role" in df else []) + ["overall_score"] + list(weights)
        return [title + f"  weights: {weights}", df[cols].to_string(index=False), ""]

    L = [f"C3 scorecard (run {run_date}); thresholds: {sc}", "=" * 110]
    L += block("IN-NETWORK", inn, "file_name", W_IN)
    L += ["in-network raw: " + inn.drop(columns=list(W_IN)).to_string(index=False), ""]
    L += block("ALLOWED-AMOUNTS", aa, "file_name", W_AA)
    L += ["allowed raw: " + aa.drop(columns=list(W_AA)).to_string(index=False), ""]
    L += block("INDEX", ix, "index_name", W_IX)
    L += ["index raw: " + ix.drop(columns=list(W_IX)).to_string(index=False)]
    (s3 / "scorecard_report.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
