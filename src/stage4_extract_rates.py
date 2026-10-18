"""C4: extract the PPO network's rates for EVERY provider, decode what each price is, and keep one rate per service.

Input : data/stage1/files.parquet (in_scope) -> in-network file(s) and allowed-amounts file(s) in data/raw/rates/
        HiLabs Excel (config paths.hilabs_xlsx), sheet "Spec-Proc Weights" -> the benchmark code list
Output: data/stage4/rates_all.parquet  every in-network price, every code type, every provider (complete table)
        data/stage4/rates.parquet (+ .csv)  the HiLabs-code subset (professional AND facility) used by C6 / C7
        data/stage4/oon_allowed.parquet (+ .csv)  out-of-network allowed amounts (never mixed into in-network rates)
        data/stage4/oon_vs_in_network.parquet  per code: in-network rate vs out-of-network allowed amount
        data/stage4/c4_report.txt

Method (logic D4.3-D4.10, docs/logic_ppo_network.md section 5):
  1 every price of every code type is extracted (not only HiLabs codes), so every provider in the file gets rates
  2 facility prices are decoded: a revenue-code (RC) item whose price carries a CPT / HCPCS code in
    additional_information is a facility price FOR THAT CPT / HCPCS service; billing_code = that code, the RC is kept
    as revenue_code. RC items without an embedded code stay revenue-code rates (room and board, per diems).
  3 one service = (npi, tin, code type, code, item name, billing class, modifier, place of service, revenue code,
    arrangement). Professional vs facility, place of service, modifier and capitation vs fee-for-service are
    different services, never collapsed.
  4 several contracts (provider_references) pricing the same service for the same provider: rate = the price in the
    group's PRIMARY contract (the provider_reference that prices the most services in the file), a real contracted
    price; ties / one contract with several prices -> median. rate_median_all / min / max / n_contracts kept for audit.
  5 only dollar amounts (negotiated, fee schedule, derived); drop <= $0 and contracts expired before the file's own
    last_updated_on date (not today's date, so re-runs are reproducible)

Usage: python -m src.stage4_extract_rates
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

import ijson
import pandas as pd

from src.common.normalize import load_config, norm_npi, norm_tin, save_csv
from src.stage1_ingest_index import download
from src.stage4_tin_npi_pairs import business_label

DOLLAR_TYPES = {"negotiated", "fee schedule", "derived"}
CPT_RX, HCPCS_RX = re.compile(r"\d{4}[0-9A-Z]"), re.compile(r"[A-Z]\d{4}")
SVC_COLS = ["billing_code_type", "billing_code", "item_name", "billing_class", "modifier", "service_code",
            "revenue_code", "negotiation_arrangement"]
KEY_COLS = ["npi", "tin"] + SVC_COLS


def load_hilabs_codes(cfg) -> pd.DataFrame:
    sp = pd.read_excel(cfg["paths"]["hilabs_xlsx"], sheet_name="Spec-Proc Weights", dtype=str)
    return sp.groupby("PROC_CD").agg(hilabs_spec_cds=("SPEC_CD", lambda s: ",".join(sorted(set(s))))).reset_index()


def file_date(path: Path) -> str:
    head = path.open("rb").read(4096).decode("utf-8", "ignore")
    m = re.search(r'"last_updated_on"\s*:\s*"([^"]+)"', head)
    return m.group(1) if m else "0000-00-00"


def decode(ct: str, code: str, addl) -> tuple[str, str, str]:
    """(billing_code_type, billing_code, revenue_code). Facility RC prices with an embedded CPT / HCPCS code are
    priced for that code."""
    if ct == "RC":
        a = (addl or "").strip().upper()
        if CPT_RX.fullmatch(a):
            return "CPT", a, code
        if HCPCS_RX.fullmatch(a):
            return "HCPCS", a, code
        return "RC", code, code
    return ct, code, ""


def read_refs(path: Path) -> dict:
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


def extract(path: Path, file_key: str):
    """Contract-level aggregation first (memory-light), then expansion to NPIs."""
    refs = read_refs(path)
    asof = file_date(path)
    drop = Counter()
    acc = defaultdict(list)          # (ref, svc...) -> rates
    meta = {}                        # (ref, svc...) -> [negotiated types, max expiration, item description]
    breadth = defaultdict(set)       # ref -> services it prices (primary-contract rule)
    with open(path, "rb") as f:
        for item in ijson.items(f, "in_network.item", use_float=True):
            ct, code = item.get("billing_code_type"), str(item.get("billing_code"))
            name, arr = item.get("name") or item.get("description"), item.get("negotiation_arrangement") or ""
            for b in item.get("negotiated_rates") or []:
                rlist = b.get("provider_references") or []
                for p in b.get("negotiated_prices") or []:
                    nt, rate, exp = p.get("negotiated_type"), p.get("negotiated_rate"), p.get("expiration_date")
                    if nt not in DOLLAR_TYPES:
                        drop[f"price: non-dollar type ({nt})"] += 1
                        continue
                    if rate is None or float(rate) <= 0:
                        drop["price: rate <= 0"] += 1
                        continue
                    if exp and exp < asof:
                        drop["price: expired before the file date"] += 1
                        continue
                    bct, bcode, rc = decode(ct, code, p.get("additional_information"))
                    # a decoded facility price belongs to the embedded code, not to the RC item that carries it:
                    # the item name (e.g. "Emergency Room General") is kept in source_items, not in the key
                    svc = (bct, bcode, "" if bct != ct else name, p.get("billing_class"), "|".join(sorted(p.get("billing_code_modifier") or [])),
                           "|".join(sorted(p.get("service_code") or [])), rc, arr)
                    for r in rlist:
                        acc[(r,) + svc].append(float(rate))
                        m = meta.setdefault((r,) + svc, [set(), "", set()])
                        m[0].add(nt)
                        m[1] = max(m[1], exp or "")
                        m[2].add(f"{ct} {code} {name}")
                        breadth[r].add(svc)
    # contract-level rows
    crow = [(k[0],) + k[1:] + (median(v), min(v), max(v), len(v), "|".join(sorted(meta[k][0])), meta[k][1],
             "; ".join(sorted(meta[k][2])))
            for k, v in acc.items()]
    c = pd.DataFrame(crow, columns=["ref"] + SVC_COLS + ["ref_rate", "ref_min", "ref_max", "ref_n", "negotiated_types",
                                                         "expiration_date", "source_items"])
    c["ref_breadth"] = c.ref.map(lambda r: len(breadth[r]))
    # expand contracts to (npi, tin)
    rp = pd.DataFrame([(r, n, t, tt, lab) for r, rows in refs.items() for n, t, tt, lab in rows],
                      columns=["ref", "npi", "tin", "tin_type", "network_hint"]).drop_duplicates()
    x = c.merge(rp, on="ref")
    x["_npi"] = x.npi.fillna("")
    k = ["_npi", "tin"] + SVC_COLS
    # primary contract per TIN: the provider_reference pricing the most services for that TIN
    x["_rank"] = x.groupby(["_npi", "tin"] + SVC_COLS).ref_breadth.rank(method="min", ascending=False)
    prim = x[x._rank == 1].groupby(k, dropna=False).agg(rate=("ref_rate", "median"),
                                                       primary_contract=("ref", lambda s: int(min(s)))).reset_index()
    g = x.groupby(k, dropna=False).agg(
        rate_median_all=("ref_rate", "median"), rate_min=("ref_min", "min"), rate_max=("ref_max", "max"),
        n_rates=("ref_n", "sum"), n_contracts=("ref", "nunique"),
        n_distinct_rates=("ref_rate", lambda s: s.round(2).nunique()),
        negotiated_types=("negotiated_types", lambda s: "|".join(sorted(set("|".join(s).split("|"))))),
        expiration_date=("expiration_date", "max"),
        network_hint=("network_hint", lambda s: "|".join(sorted({v for v in s if v}))),
        tin_type=("tin_type", "first"),
        source_items=("source_items", lambda s: "; ".join(sorted({v for x in s for v in x.split("; ")})))).reset_index()
    out = g.merge(prim, on=k, how="left")
    out["npi"] = out._npi.replace("", None)
    out = out.drop(columns="_npi")
    out["rate_source"] = out.n_contracts.map(lambda n: "single contract" if n == 1 else f"primary of {n} contracts")
    out["component"] = out.billing_class.map({"professional": "professional", "institutional": "facility"})
    out["in_capitation_code"] = out.negotiation_arrangement.eq("capitation")
    out["item_name"] = out.item_name.where(out.item_name.ne(""), "facility price under revenue code " + out.revenue_code)
    out["rate_spread_ratio"] = (out.rate_max / out.rate_min).round(3)
    out["file_key"] = file_key
    out["file_date"] = asof
    return out, drop


def read_oon(path: Path, file_key: str) -> pd.DataFrame:
    """Out-of-network allowed amounts: one row per (code, tin, npi, payment)."""
    rows = []
    d = json.loads(path.read_text(encoding="utf-8"))
    for it in d.get("out_of_network") or []:
        for a in it.get("allowed_amounts") or []:
            for p in a.get("payments") or []:
                for pr in p.get("providers") or []:
                    for n in pr.get("npi") or []:
                        rows.append({"billing_code_type": it.get("billing_code_type"), "billing_code": str(it.get("billing_code")),
                                     "item_name": it.get("name"), "tin": norm_tin((a.get("tin") or {}).get("value")),
                                     "npi": norm_npi(n), "service_code": "|".join(sorted(a.get("service_code") or [])),
                                     "billing_class": a.get("billing_class"), "allowed_amount": p.get("allowed_amount"),
                                     "billed_charge": pr.get("billed_charge"), "file_key": file_key})
    return pd.DataFrame(rows)


def main(argv=None):
    cfg = load_config()
    out_dir = cfg["paths"]["stage4"]
    out_dir.mkdir(parents=True, exist_ok=True)
    hl = load_hilabs_codes(cfg)
    hil = set(hl.PROC_CD)
    files = pd.read_parquet(cfg["paths"]["stage1"] / "files.parquet")
    sel = files[files.in_scope]
    parts, oon, drops = [], [], Counter()
    for r in sel.itertuples():
        dest = cfg["paths"]["raw_rates"] / r.file_name
        if not dest.exists():
            download(r.example_url, dest, cfg)
        if r.file_role == "in_network":
            print(f"extracting {r.file_name}")
            df, d = extract(dest, r.file_key)
            parts.append(df)
            drops.update(d)
        elif r.file_role == "allowed_amount":
            print(f"reading out-of-network allowed amounts {r.file_name}")
            oon.append(read_oon(dest, r.file_key))
    allr = pd.concat(parts, ignore_index=True).merge(hl, left_on="billing_code", right_on="PROC_CD", how="left") \
        .drop(columns="PROC_CD")
    allr["is_hilabs_code"] = allr.billing_code.isin(hil)
    assert not allr.duplicated(["npi", "tin"] + SVC_COLS + ["file_key"]).any()
    assert (allr.rate > 0).all()
    allr.to_parquet(out_dir / "rates_all.parquet", index=False)
    rates = allr[allr.is_hilabs_code].drop(columns="is_hilabs_code").reset_index(drop=True)
    rates.to_parquet(out_dir / "rates.parquet", index=False)
    save_csv(rates, out_dir / "rates.csv")

    # out-of-network: separate table + benchmark vs in-network (same code, same billing class)
    o = pd.concat(oon, ignore_index=True) if oon else pd.DataFrame()
    if len(o):
        o.to_parquet(out_dir / "oon_allowed.parquet", index=False)
        save_csv(o, out_dir / "oon_allowed.csv")
        inn = allr[allr.negotiation_arrangement.ne("capitation")].groupby(["billing_code", "billing_class"]).agg(
            in_network_median=("rate", "median"), in_network_providers=("npi", "nunique")).reset_index()
        oo = o.groupby(["billing_code", "billing_class"]).agg(
            oon_allowed_median=("allowed_amount", "median"), oon_billed_median=("billed_charge", "median"),
            oon_payments=("allowed_amount", "size"), oon_providers=("npi", "nunique")).reset_index()
        bench = inn.merge(oo, on=["billing_code", "billing_class"])
        bench["oon_to_in_network_ratio"] = (bench.oon_allowed_median / bench.in_network_median).round(2)
        bench["is_hilabs_code"] = bench.billing_code.isin(hil)
        bench.to_parquet(out_dir / "oon_vs_in_network.parquet", index=False)

    prof = rates[rates.component == "professional"]
    fac = rates[rates.component == "facility"]
    sp = pd.read_excel(cfg["paths"]["hilabs_xlsx"], sheet_name="Spec-Proc Weights", dtype=str)
    w = sp.assign(w=sp.SPEC_PROC_WEIGHT.astype(float)).groupby("PROC_CD").w.sum()
    cov = lambda codes: round(100 * w.reindex(list(codes)).fillna(0).sum() / w.sum(), 1)  # noqa: E731
    pairs = pd.read_parquet(out_dir / "tin_npi_pairs.parquet")
    L = [f"C4 report (file date {allr.file_date.iloc[0]})", "=" * 78,
         f"ALL in-network prices extracted: {len(allr):,} rows | NPIs {allr.npi.nunique():,} of {pairs.npi.nunique():,} "
         f"in the file | TINs {allr.tin.nunique():,} | codes {allr.billing_code.nunique():,}",
         "   by code type: " + str(allr.billing_code_type.value_counts().to_dict())
         + " | by component: " + str(allr.component.value_counts().to_dict()),
         f"Facility prices decoded from additional_information (RC item priced for a CPT/HCPCS code): "
         f"{int((allr.revenue_code.ne('') & allr.billing_code_type.ne('RC')).sum()):,} rows",
         "",
         f"HiLabs-code rates (rates.parquet): {len(rates):,} rows | NPIs {rates.npi.nunique():,} | codes "
         f"{rates.billing_code.nunique()} ({cov(rates.billing_code.unique())}% of HiLabs weight)",
         f"   professional: {len(prof):,} rows, {prof.billing_code.nunique()} codes ({cov(prof.billing_code.unique())}% weight), "
         f"{prof.npi.nunique():,} NPIs",
         f"   facility:     {len(fac):,} rows, {fac.billing_code.nunique()} codes ({cov(fac.billing_code.unique())}% weight), "
         f"{fac.npi.nunique():,} NPIs; codes priced ONLY at facility level: "
         f"{len(set(fac.billing_code) - set(prof.billing_code))}",
         "",
         "Several rates for one service (rule D4.6):",
         f"   services priced by more than one contract: {int((rates.n_contracts > 1).sum()):,} rows -> primary-contract rate",
         f"   rows where the primary-contract rate differs from the median of all contracts: "
         f"{int(((rates.rate - rates.rate_median_all).abs() > 0.005).sum()):,}",
         f"   capitation (HMO-style) rows, kept but never the headline rate: {int(rates.in_capitation_code.sum()):,}",
         ""]
    if len(o):
        L += [f"Out-of-network allowed amounts: {len(o):,} payments | codes {o.billing_code.nunique()} | TINs "
              f"{o.tin.nunique()} | NPIs {o.npi.nunique()}",
              f"   codes priced both in- and out-of-network: {len(bench)} | median out-of-network allowed / in-network "
              f"rate: {bench.oon_to_in_network_ratio.median():.2f}x",
              f"   (NPI, TIN) pairs listed in-network but paid out-of-network: "
              f"{o.merge(pairs[['npi', 'tin']], on=['npi', 'tin'])[['npi', 'tin']].drop_duplicates().shape[0]}", ""]
    L += ["dropped (price entries, before expanding to providers):"] + ([f"  {v:>10,}  {k}" for k, v in sorted(drops.items())]
                                                                        or ["  none: every price is a positive dollar amount, none expired"])
    (out_dir / "c4_report.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
