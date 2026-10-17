"""C4: extract PPO-network rates for the HiLabs billing codes, one row per provider x service.

Input : data/stage1/files.parquet (in_scope & in_network) -> data/raw/rates/<file_name>
        HiLabs Excel (config paths.hilabs_xlsx), sheet "Spec-Proc Weights" -> the billing codes to keep
Output: data/stage4/rates.parquet (+ .csv) and data/stage4/c4_report.txt

Which-rate rule (logic D4.3-D4.6):
  1. keep only billing codes in the HiLabs list (CPT / HCPCS)
  2. keep only dollar amounts: negotiated_type in {negotiated, fee schedule, derived};
     percentage / per diem are counted and reported, not mixed in
  3. drop rates <= 0 and expired rates (expiration_date < today)
  4. one row per (npi, tin, code_type, code, item_name, billing_class, modifiers, place of service,
     additional_information); rate = median of what is left, with min / max / count kept

Usage (from project root):
  python -m src.stage4_extract_rates
  python -m src.stage4_extract_rates --file tests/sample_in_network.json   # test on one local file
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from statistics import median

import ijson
import pandas as pd

from src.common.normalize import ROOT, load_config, norm_npi, norm_tin
from src.stage1_ingest_index import download
from src.stage4_tin_npi_pairs import business_label

DOLLAR_TYPES = {"negotiated", "fee schedule", "derived"}
KEY_COLS = ["npi", "tin", "billing_code_type", "billing_code", "item_name", "billing_class",
            "modifier", "service_code", "additional_information"]


def load_hilabs_codes(cfg) -> pd.DataFrame:
    sp = pd.read_excel(cfg["paths"]["hilabs_xlsx"], sheet_name="Spec-Proc Weights", dtype=str)
    return sp.groupby("PROC_CD").agg(hilabs_spec_cds=("SPEC_CD", lambda s: ",".join(sorted(set(s))))).reset_index()


def read_refs(path: Path) -> dict:
    """provider_group_id -> list of (npi or None, tin, tin_type, label)."""
    refs = {}
    with open(path, "rb") as f:
        for ref in ijson.items(f, "provider_references.item", use_float=True):
            rows = []
            for g in ref.get("provider_groups") or []:
                t = g.get("tin") or {}
                tin, label = norm_tin(t.get("value")), business_label(t.get("business_name") or "")
                for raw in g.get("npi") or []:
                    rows.append((norm_npi(raw), tin, t.get("type"), label))
            refs[ref.get("provider_group_id")] = rows
    return refs


def extract(path: Path, file_key: str, codes: set[str], today: str):
    refs = read_refs(path)
    drop = Counter()
    seen_codes = set()
    acc = defaultdict(list)              # key -> [rates]
    meta = {}                            # key -> (negotiated_types set, max expiration, arrangement, network_hint)
    with open(path, "rb") as f:
        for item in ijson.items(f, "in_network.item", use_float=True):
            code = str(item.get("billing_code"))
            if code not in codes:
                drop["item: billing code not in HiLabs list"] += 1
                continue
            seen_codes.add(code)
            ctype, name = item.get("billing_code_type"), item.get("name") or item.get("description")
            arrangement = item.get("negotiation_arrangement")
            for b in item.get("negotiated_rates") or []:
                providers = [p for r in b.get("provider_references") or [] for p in refs.get(r, [])]
                for p in b.get("negotiated_prices") or []:
                    nt, rate, exp = p.get("negotiated_type"), p.get("negotiated_rate"), p.get("expiration_date")
                    if nt not in DOLLAR_TYPES:
                        drop[f"price: non-dollar type ({nt})"] += 1
                        continue
                    if rate is None or float(rate) <= 0:
                        drop["price: rate <= 0"] += 1
                        continue
                    if exp and exp < today:
                        drop["price: expired"] += 1
                        continue
                    mods = "|".join(sorted(p.get("billing_code_modifier") or []))
                    pos = "|".join(sorted(p.get("service_code") or []))
                    for npi, tin, tin_type, label in providers:
                        k = (npi, tin, ctype, code, name, p.get("billing_class"), mods, pos,
                             p.get("additional_information"))
                        acc[k].append(float(rate))
                        m = meta.setdefault(k, [set(), "", set(), set(), tin_type])
                        m[0].add(nt)
                        m[1] = max(m[1], exp or "")
                        m[2].add(arrangement)
                        if label:
                            m[3].add(label)
    rows = []
    for k, v in acc.items():
        m = meta[k]
        rows.append((*k, median(v), min(v), max(v), len(v), len(set(v)), "|".join(sorted(m[0])), m[1],
                     "|".join(sorted(a for a in m[2] if a)), "|".join(sorted(m[3])), m[4]))
    df = pd.DataFrame(rows, columns=KEY_COLS + ["rate", "rate_min", "rate_max", "n_rates", "n_distinct_rates",
                                                "negotiated_types", "expiration_date", "negotiation_arrangement",
                                                "network_hint", "tin_type"])
    df["in_capitation_code"] = df.negotiation_arrangement.str.contains("capitation")
    df["rate_spread_ratio"] = (df.rate_max / df.rate_min).round(3)  # 1.0 = all prices identical; for C3 outliers
    df["file_key"] = file_key
    return df, drop, seen_codes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="process one local rate file instead of the in-scope files from C1")
    args = ap.parse_args(argv)
    cfg = load_config()
    out_dir = cfg["paths"]["stage4"]
    today = date.today().isoformat()

    hl = load_hilabs_codes(cfg)
    codes = set(hl.PROC_CD)
    if args.file:
        jobs = [(ROOT / args.file, "local:" + Path(args.file).name)]
        out_dir = out_dir.parent / "stage4_sample"
    else:
        files = pd.read_parquet(cfg["paths"]["stage1"] / "files.parquet")
        sel = files[files.in_scope & (files.file_role == "in_network")]
        jobs = []
        for r in sel.itertuples():
            dest = cfg["paths"]["raw_rates"] / r.file_name
            if not dest.exists():
                download(r.example_url, dest, cfg)
            jobs.append((dest, r.file_key))
    out_dir.mkdir(parents=True, exist_ok=True)

    parts, drops, seen = [], Counter(), set()
    for path, key in jobs:
        print(f"extracting {path.name}")
        df, d, s = extract(path, key, codes, today)
        parts.append(df)
        drops.update(d)
        seen |= s
    rates = pd.concat(parts, ignore_index=True).merge(hl, left_on="billing_code", right_on="PROC_CD",
                                                       how="left").drop(columns="PROC_CD")
    assert not rates.duplicated(KEY_COLS + ["file_key"]).any()
    assert (rates.rate > 0).all()
    rates.to_parquet(out_dir / "rates.parquet", index=False)
    rates.to_csv(out_dir / "rates.csv", index=False, encoding="utf-8")

    hmo_hint = rates.network_hint.str.contains("AMBETTER_HMO|IPA") | rates.in_capitation_code
    L = [f"C4 report (rates for HiLabs codes; run date {today})", "=" * 70,
         f"HiLabs codes: {len(codes)} | found in file(s): {len(seen)} | not found: {len(codes - seen)}",
         f"rate rows: {len(rates):,}",
         f"unique NPIs: {rates.npi.nunique():,} | unique TINs: {rates.tin.nunique():,} | "
         f"(NPI,TIN) pairs: {rates[['npi','tin']].drop_duplicates().shape[0]:,} | "
         f"TIN-only rows (no NPI): {int(rates.npi.isna().sum())}",
         f"billing codes with rates: {rates.billing_code.nunique()} | items (code+name): "
         f"{rates[['billing_code','item_name']].drop_duplicates().shape[0]}",
         f"billing_class: {rates.billing_class.value_counts().to_dict()}",
         f"negotiated_types: {rates.negotiated_types.value_counts().to_dict()}",
         f"rows with >1 rate before median: {int((rates.n_rates > 1).sum()):,} | "
         f"with >1 DISTINCT rate: {int((rates.n_distinct_rates > 1).sum()):,}",
         f"rows with rate_spread_ratio > 3 (max/min): {int((rates.rate_spread_ratio > 3).sum()):,}",
         f"rate quantiles: " + ", ".join(f"{q}={rates.rate.quantile(q):.2f}" for q in (0.01, .25, .5, .75, .99)),
         "",
         "D2.7 sensitivity (HMO-style hints: AMBETTER_HMO, IPA, capitation code):",
         f"  rows with hint: {int(hmo_hint.sum()):,} | rows without: {int((~hmo_hint).sum()):,}",
         f"  NPIs without hinted rows: {rates[~hmo_hint].npi.nunique():,}",
         "network_hint counts (rows): " + str(rates.network_hint.replace('', '(none)').value_counts().to_dict()),
         "", "dropped (counts are items or price entries, before expanding to providers):"]
    L += [f"  {v:>10,}  {k}" for k, v in sorted(drops.items())]
    text = "\n".join(L) + "\n"
    (out_dir / "c4_report.txt").write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {out_dir / 'rates.parquet'}")


if __name__ == "__main__":
    main()
