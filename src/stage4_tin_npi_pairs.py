"""Hand-off for Member B: every (NPI, TIN) pair in the in-scope in-network rate files.

Reads only what B needs: provider groups (TIN + NPI list + business name), plus cheap helper
columns from one pass over the prices (how many codes, which billing classes, capitation).
No rates are extracted here (that is C4).

Input : data/stage1/files.parquet (in_scope & in_network) -> data/raw/rates/<file_name>
        (downloaded if missing)
Output: data/stage4/tin_npi_pairs.parquet (+ .csv), one row per (npi, tin, file_key)

Usage (from project root):
  python -m src.stage4_tin_npi_pairs
  python -m src.stage4_tin_npi_pairs --file tests/sample_in_network.json   # test on one local file
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

import ijson
import pandas as pd

from src.common.normalize import ROOT, load_config, norm_npi, norm_tin, save_csv
from src.stage1_ingest_index import download

# Provider-level network hints written into tin.business_name (see logic D2.1). Order matters: first match wins.
LABEL_RULES = [
    ("AMBETTER_HMO", re.compile(r"^\s*DIRECT NETWORK:\s*AMBETTER HMO", re.I)),
    ("AMBETTER_PPO", re.compile(r"^\s*DIRECT NETWORK:\s*AMBETTER PPO", re.I)),
    ("DIRECT_NETWORK", re.compile(r"^\s*DIRECT NETWORK:", re.I)),
    ("PPO_COUNTY", re.compile(r"^\s*PPO:", re.I)),
    ("HEALTHNET_PPO", re.compile(r"HEALTH\s*NET PPO", re.I)),
    ("HEALTHNET_DIRECT_FFS_COMMERCIAL", re.compile(r"HEALTH\s*NET DIRECT FFS COMMERCIAL", re.I)),
    ("IPA", re.compile(r"\bIPA\b", re.I)),
]


def business_label(name: str) -> str:
    for label, rx in LABEL_RULES:
        if rx.search(name or ""):
            return label
    return ""


def read_pairs(path: Path, file_key: str) -> pd.DataFrame:
    # pass 1: provider_references -> (ref_id, npi, tin, tin_type, business_name)
    rows, bad_npi, bad_tin = [], 0, 0
    with open(path, "rb") as f:
        for ref in ijson.items(f, "provider_references.item", use_float=True):
            rid = ref.get("provider_group_id")
            for g in ref.get("provider_groups") or []:
                t = g.get("tin") or {}
                tin = norm_tin(t.get("value"))
                bn = (t.get("business_name") or "").strip()
                if tin is None:
                    bad_tin += 1
                for raw in g.get("npi") or []:
                    npi = norm_npi(raw)
                    if npi is None:  # e.g. placeholder 0: TIN-only contract. Kept as a row with npi = None
                        bad_npi += 1
                    rows.append((rid, npi, tin, t.get("type"), bn))
    refs = pd.DataFrame(rows, columns=["ref_id", "npi", "tin", "tin_type", "business_name"])

    # pass 2: per provider_reference, which codes / billing classes / arrangements it is priced for
    codes, classes, capit = defaultdict(set), defaultdict(set), defaultdict(bool)
    with open(path, "rb") as f:
        for item in ijson.items(f, "in_network.item", use_float=True):
            code = f"{item.get('billing_code_type')}:{item.get('billing_code')}"
            is_cap = item.get("negotiation_arrangement") == "capitation"
            for b in item.get("negotiated_rates") or []:
                bcs = {p.get("billing_class") for p in b.get("negotiated_prices") or []}
                for r in b.get("provider_references") or []:
                    codes[r].add(code)
                    classes[r] |= bcs
                    capit[r] |= is_cap
    refs["n_codes_ref"] = refs.ref_id.map(lambda r: len(codes.get(r, ())))
    refs["classes"] = refs.ref_id.map(lambda r: classes.get(r, set()))
    refs["capitation"] = refs.ref_id.map(lambda r: capit.get(r, False))
    refs["label"] = refs.business_name.map(business_label)

    # aggregate to one row per (npi, tin)
    def uniq(s):
        return " | ".join(sorted({x for x in s if x}))

    pair_codes = defaultdict(set)
    for r, npi, tin in refs[["ref_id", "npi", "tin"]].itertuples(index=False):
        pair_codes[(npi, tin)] |= codes.get(r, set())
    g = refs.groupby(["npi", "tin"], dropna=False)
    out = g.agg(tin_type=("tin_type", "first"),
                business_names=("business_name", uniq),
                business_name_labels=("label", uniq),
                n_provider_groups=("ref_id", "nunique"),
                provider_group_ids=("ref_id", lambda s: ",".join(map(str, sorted(set(s))))),
                in_capitation_code=("capitation", "max"),
                billing_classes=("classes", lambda s: " | ".join(sorted({c for x in s for c in x if c}))),
                ).reset_index()
    out["n_billing_codes"] = [len(pair_codes[(a, b)]) for a, b in zip(out.npi, out.tin)]
    out["npi_missing"] = out.npi.isna()
    out["n_npis_on_tin"] = out.groupby("tin").npi.transform("nunique")
    out["n_tins_for_npi"] = out.groupby("npi").tin.transform("nunique").astype("Int64")  # <NA> for TIN-only rows
    out["file_key"] = file_key
    out.attrs["bad_npi"], out.attrs["bad_tin"] = bad_npi, bad_tin
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", help="process one local rate file instead of the in-scope files from C1")
    args = ap.parse_args(argv)
    cfg = load_config()
    out_dir = cfg["paths"]["stage4"]

    if args.file:
        jobs = [(ROOT / args.file, "local:" + Path(args.file).name)]
        out_dir = out_dir.parent / "stage4_sample"
    else:
        files = pd.read_parquet(cfg["paths"]["stage1"] / "files.parquet")
        sel = files[files.in_scope & (files.file_role == "in_network")]
        if sel.empty:
            raise SystemExit("No in-scope in-network files in files.parquet; run stage1 first")
        jobs = []
        for r in sel.itertuples():
            dest = cfg["paths"]["raw_rates"] / r.file_name
            if not dest.exists():
                print(f"downloading {r.file_name}")
                download(r.example_url, dest, cfg)
            jobs.append((dest, r.file_key))
    out_dir.mkdir(parents=True, exist_ok=True)

    parts = []
    for path, key in jobs:
        print(f"reading provider groups from {path.name}")
        df = read_pairs(path, key)
        print(f"  pairs={len(df):,} npis={df.npi.nunique():,} tins={df.tin.nunique():,} "
              f"invalid npi values={df.attrs['bad_npi']} invalid tin values={df.attrs['bad_tin']}")
        parts.append(df)
    pairs = pd.concat(parts, ignore_index=True)

    assert pairs.npi.dropna().str.fullmatch(r"\d{10}").all(), "NPI must be a 10-digit string"
    assert pairs.tin.dropna().str.fullmatch(r"\d{9}").all(), "TIN must be a 9-digit string"
    assert not pairs.duplicated(["npi", "tin", "file_key"]).any()

    pairs.to_parquet(out_dir / "tin_npi_pairs.parquet", index=False)
    save_csv(pairs, out_dir / "tin_npi_pairs.csv")

    lab = pairs.business_name_labels.replace("", "(none)").value_counts()
    print(f"\nwrote {out_dir / 'tin_npi_pairs.parquet'}")
    print(f"rows={len(pairs):,}  unique NPIs={pairs.npi.nunique():,}  unique TINs={pairs.tin.nunique():,}  "
          f"TIN-only rows (no valid NPI)={int(pairs.npi_missing.sum())}")
    print(f"NPIs under >1 TIN: {(pairs.groupby('npi').tin.nunique() > 1).sum():,}   "
          f"TINs with >1 NPI: {(pairs.groupby('tin').npi.nunique() > 1).sum():,}   "
          f"max NPIs on one TIN: {pairs.n_npis_on_tin.max()}")
    print("billing classes:", pairs.billing_classes.value_counts().to_dict())
    print("pairs in a capitation code:", int(pairs.in_capitation_code.sum()))
    print("business-name labels:", lab.to_dict())


if __name__ == "__main__":
    main()
