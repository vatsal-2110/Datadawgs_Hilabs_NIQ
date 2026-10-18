"""C3: grade the quality and completeness of PTD in-network files.

Graded files: every in-scope in-network file (the file we USE) plus comparison files from the same payer
(config grading.compare_files), so each score can be read against a peer.

Each file is streamed with ijson (never loaded whole); files are graded in PARALLEL (one process each).

Metrics (each is a percentage 0-100; formulas and reasons in docs/C3_file_quality_metrics.md):
  M1 code_coverage     HiLabs code weight priced (incl. facility prices decoded from RC items) / total weight (25)
  M2 provider_detail   share of NPIs that are individuals (Type 1), NPPES sample              (weight 15)
  M3 rate_validity     100 - %prices <= 0 - %robust outliers (|modified z| > 3.5 in-service)  (weight 15)
  M4 usable_rate_types % prices in dollars (negotiated / fee schedule / derived)              (weight 10)
  M5 uniqueness        100 - %duplicate price entries - %conflicting prices per provider/svc (weight 10)
  M6 freshness         100 if last_updated_on <= 31 days old, linear to 0 at 365 days         (weight 10)
  M7 id_validity       mean(%NPIs passing the NPI check digit, %TINs with 9 digits)          (weight 15)
Reported, not scored: expiry-date informativeness, institutional share, capitation items, network_name fill.

Usage (from project root):
  python -m src.stage3_grade_files
"""
from __future__ import annotations

import json
import random
import re
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path
from statistics import median

import ijson
import pandas as pd

from src.common.normalize import load_config, norm_npi, save_csv
from src.stage1_ingest_index import download

WEIGHTS = {"M1_code_coverage": 25, "M2_provider_detail": 15, "M3_rate_validity": 15,
           "M4_usable_rate_types": 10, "M5_uniqueness": 10, "M6_freshness": 10, "M7_id_validity": 15}
DOLLAR = {"negotiated", "fee schedule", "derived"}
NPPES_SAMPLE = 200


def npi_check_digit_ok(npi: str) -> bool:
    """CMS NPI check digit: Luhn over '80840' + first 9 digits."""
    if not re.fullmatch(r"\d{10}", npi or ""):
        return False
    digits = [int(c) for c in "80840" + npi[:9]]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:
            d *= 2
            d = d - 9 if d > 9 else d
        total += d
    return (10 - total % 10) % 10 == int(npi[9])


def robust_outliers(values):
    """Count |modified z| > 3.5 (Iglewicz & Hoaglin) in one service group; groups < 10 prices are skipped."""
    if len(values) < 10:
        return 0, 0
    m = median(values)
    mad = median(abs(v - m) for v in values)
    if mad == 0:
        return sum(1 for v in values if v != m and abs(v - m) > 0.5 * m), len(values)  # flat groups: >50% off
    return sum(1 for v in values if abs(0.6745 * (v - m) / mad) > 3.5), len(values)


def grade_one(args):
    path, file_key, file_name, codes_w, run_date = args
    path = Path(path)
    hdr = {}
    with open(path, "rb") as f:
        head = f.read(8192).decode("utf-8", "ignore")
    for k in ("last_updated_on", "version", "reporting_entity_name"):
        m = re.search(rf'"{k}"\s*:\s*"([^"]*)"', head)
        hdr[k] = m.group(1) if m else None

    # pass 1: provider references
    npis, tins, n_npi_entries, bad_npi, bad_tin, n_tin, refs_netname, n_refs = set(), set(), 0, 0, 0, 0, 0, 0
    with open(path, "rb") as f:
        for r in ijson.items(f, "provider_references.item", use_float=True):
            n_refs += 1
            if [x for x in (r.get("network_name") or []) if x]:
                refs_netname += 1
            for g in r.get("provider_groups") or []:
                tv = re.sub(r"\D", "", str((g.get("tin") or {}).get("value") or ""))
                n_tin += 1
                if len(tv) != 9:
                    bad_tin += 1
                tins.add(tv)
                for raw in g.get("npi") or []:
                    n_npi_entries += 1
                    s = norm_npi(raw)
                    if s is None or not npi_check_digit_ok(s):
                        bad_npi += 1
                    if s:
                        npis.add(s)

    # pass 2: in_network items
    c = defaultdict(Counter)
    n_items = n_prices = n_zero = n_dollar = 0
    priced_codes = set()
    groups = defaultdict(list)            # service -> prices (for outliers)
    entry_seen = Counter()                # exact duplicate price entries
    svc_rates = defaultdict(set)          # (ref, service) -> distinct rates (conflicts)
    real_expiry = 0
    with open(path, "rb") as f:
        for it in ijson.items(f, "in_network.item", use_float=True):
            n_items += 1
            code = str(it.get("billing_code"))
            c["arrangement"][it.get("negotiation_arrangement")] += 1
            name = it.get("name")
            for b in it.get("negotiated_rates") or []:
                refs = tuple(sorted(b.get("provider_references") or []))
                for p in b.get("negotiated_prices") or []:
                    n_prices += 1
                    nt, rate, bc = p.get("negotiated_type"), p.get("negotiated_rate"), p.get("billing_class")
                    c["billing_class"][bc] += 1
                    c["negotiated_type"][nt] += 1
                    if (p.get("expiration_date") or "9999-12-31") != "9999-12-31":
                        real_expiry += 1
                    rate = float(rate) if rate is not None else None
                    if rate is None or rate <= 0:
                        n_zero += 1
                    if nt in DOLLAR:
                        n_dollar += 1
                        if rate and rate > 0:
                            priced_codes.add(code)
                            # facility price filed under a revenue code but naming its CPT/HCPCS code (C4 decoding)
                            a = (p.get("additional_information") or "").strip().upper()
                            if it.get("billing_code_type") == "RC" and re.fullmatch(r"\d{4}[0-9A-Z]|[A-Z]\d{4}", a):
                                priced_codes.add(a)
                    svc = (it.get("billing_code_type"), code, name, bc,
                           "|".join(sorted(p.get("service_code") or [])),
                           "|".join(sorted(p.get("billing_code_modifier") or [])), p.get("additional_information"), nt)
                    if rate and rate > 0:
                        groups[svc].append(rate)
                    entry_seen[(refs, svc, rate)] += 1
                    for r in refs:
                        svc_rates[(r, svc)].add(rate)

    out_n = out_tot = 0
    for v in groups.values():
        o, t = robust_outliers(v)
        out_n += o
        out_tot += t
    dup_entries = sum(v - 1 for v in entry_seen.values() if v > 1)
    conflicts = sum(1 for s in svc_rates.values() if len(s) > 1)

    tot_w = sum(codes_w.values())
    cov_w = sum(w for k, w in codes_w.items() if k in priced_codes)
    hilabs_priced = len(priced_codes & set(codes_w))
    age = (date.fromisoformat(run_date) - date.fromisoformat(hdr["last_updated_on"])).days if hdr["last_updated_on"] else None

    raw = {
        "file_key": file_key, "file_name": file_name, "bytes": path.stat().st_size,
        "last_updated_on": hdr["last_updated_on"], "version": hdr["version"], "age_days": age,
        "n_provider_references": n_refs, "n_unique_npi": len(npis), "n_unique_tin": len(tins),
        "n_items": n_items, "n_prices": n_prices,
        "hilabs_codes_priced": hilabs_priced, "hilabs_codes_total": len(codes_w),
        "code_cov_count_pct": round(100 * hilabs_priced / len(codes_w), 2),
        "code_cov_weight_pct": round(100 * cov_w / tot_w, 2),
        "zero_or_missing_rate_pct": round(100 * n_zero / max(n_prices, 1), 3),
        "outlier_pct": round(100 * out_n / max(out_tot, 1), 3), "outlier_groups_prices": out_tot,
        "dollar_type_pct": round(100 * n_dollar / max(n_prices, 1), 3),
        "duplicate_entry_pct": round(100 * dup_entries / max(n_prices, 1), 3),
        "conflict_pct": round(100 * conflicts / max(len(svc_rates), 1), 3),
        "npi_check_digit_ok_pct": round(100 * (1 - bad_npi / max(n_npi_entries, 1)), 3),
        "tin_9digit_pct": round(100 * (1 - bad_tin / max(n_tin, 1)), 3),
        "network_name_filled_pct": round(100 * refs_netname / max(n_refs, 1), 2),
        "real_expiry_pct": round(100 * real_expiry / max(n_prices, 1), 3),
        "institutional_pct": round(100 * c["billing_class"].get("institutional", 0) / max(n_prices, 1), 2),
        "capitation_items": c["arrangement"].get("capitation", 0),
        "negotiated_types": dict(c["negotiated_type"]),
        "npi_list_sample": random.Random(7).sample(sorted(npis), min(NPPES_SAMPLE, len(npis))),
    }
    return raw


def type1_share(npis, cfg):
    """Share of Type 1 NPIs, using cached NPPES records (fetches any missing, in parallel)."""
    from src.stage5_fetch_sources import fetch_nppes
    npi_df, _ = fetch_nppes(list(npis), cfg)
    f = npi_df[npi_df.found]
    return round(100 * (f.enumeration_type == "NPI-1").mean(), 2), len(f)


def score(raw):
    s = {
        "M1_code_coverage": raw["code_cov_weight_pct"],
        "M2_provider_detail": raw["type1_pct"],
        "M3_rate_validity": max(0.0, 100 - raw["zero_or_missing_rate_pct"] - raw["outlier_pct"]),
        "M4_usable_rate_types": raw["dollar_type_pct"],
        "M5_uniqueness": max(0.0, 100 - raw["duplicate_entry_pct"] - raw["conflict_pct"]),
        "M6_freshness": 100.0 if raw["age_days"] is not None and raw["age_days"] <= 31 else
        max(0.0, 100 * (365 - raw["age_days"]) / (365 - 31)) if raw["age_days"] is not None else 0.0,
        "M7_id_validity": (raw["npi_check_digit_ok_pct"] + raw["tin_9digit_pct"]) / 2,
    }
    s = {k: round(v, 2) for k, v in s.items()}
    s["overall_score"] = round(sum(s[k] * w for k, w in WEIGHTS.items()) / sum(WEIGHTS.values()), 1)
    return s


def main():
    cfg = load_config()
    out = cfg["paths"]["stage3"]
    out.mkdir(parents=True, exist_ok=True)
    run_date = date.today().isoformat()
    sp = pd.read_excel(cfg["paths"]["hilabs_xlsx"], sheet_name="Spec-Proc Weights", dtype=str)
    sp["w"] = sp.SPEC_PROC_WEIGHT.astype(float)
    codes_w = sp.groupby("PROC_CD").w.sum().to_dict()

    files = pd.read_parquet(cfg["paths"]["stage1"] / "files.parquet")
    files = files[files.file_role == "in_network"]
    sel = files[files.in_scope].assign(role="USED (in scope)")
    for pat in cfg["grading"]["compare_files"]:
        sel = pd.concat([sel, files[files.file_name.str.contains(pat, regex=False)].assign(role="comparison")])
    jobs = []
    for r in sel.itertuples():
        dest = cfg["paths"]["raw_rates"] / r.file_name
        if not dest.exists():
            download(r.example_url, dest, cfg)
        jobs.append((str(dest), r.file_key, r.file_name, codes_w, run_date))

    print(f"grading {len(jobs)} files in parallel ...")
    with ProcessPoolExecutor(max_workers=min(len(jobs), 4)) as ex:
        raws = list(ex.map(grade_one, jobs))

    rows = []
    for raw, role in zip(raws, sel.role):
        raw["type1_pct"], raw["type1_sample_n"] = type1_share(raw.pop("npi_list_sample"), cfg)
        rows.append({"role": role, **{k: v for k, v in raw.items() if k != "negotiated_types"},
                     "negotiated_types": json.dumps(raw["negotiated_types"]), **score(raw)})
    df = pd.DataFrame(rows)
    df.to_parquet(out / "file_scores.parquet", index=False)
    save_csv(df, out / "file_scores.csv")

    cols = ["role", "file_name", "overall_score"] + list(WEIGHTS)
    L = [f"C3 file grades (run {run_date}); weights: {WEIGHTS}", "=" * 90,
         df[cols].to_string(index=False), "", "raw metrics:"]
    for r in df.itertuples():
        L.append(f"- {r.file_name} [{r.role}]: updated {r.last_updated_on} ({r.age_days} d), NPIs {r.n_unique_npi:,}, "
                 f"TINs {r.n_unique_tin:,}, items {r.n_items:,}, prices {r.n_prices:,}, HiLabs codes "
                 f"{r.hilabs_codes_priced}/{r.hilabs_codes_total} ({r.code_cov_count_pct}% count, "
                 f"{r.code_cov_weight_pct}% weight), Type1 {r.type1_pct}% (n={r.type1_sample_n}), $0 "
                 f"{r.zero_or_missing_rate_pct}%, outliers {r.outlier_pct}%, dollar {r.dollar_type_pct}%, dup "
                 f"{r.duplicate_entry_pct}%, conflicts {r.conflict_pct}%, NPI check-digit ok "
                 f"{r.npi_check_digit_ok_pct}%, TIN 9-digit {r.tin_9digit_pct}%, network_name filled "
                 f"{r.network_name_filled_pct}%, real expiry {r.real_expiry_pct}%, institutional "
                 f"{r.institutional_pct}%, capitation items {r.capitation_items}")
    (out / "c3_report.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
