"""C5 step 1: fetch address/taxonomy evidence for every NPI in tin_npi_pairs from public sources.

Sources (URLs in config.yaml -> sources):
  * NPPES NPI Registry API v2.1  - NPI type, names, taxonomies, practice LOCATION, secondary practice
                                    locations, MAILING, endpoint affiliation addresses (ENDPOINT),
                                    last_updated (one request per NPI)
  * CMS Doctors & Clinicians (DAC) National Downloadable File, via the CMS datastore API
                                  - Medicare-enrolled clinicians: one row per clinician x group x practice
                                    address, refreshed monthly (batches of NPIs with an 'in' query)

Requests run in parallel (config http_pool.workers). Every response is cached under data/raw/cache/,
so a re-run only fetches what is missing, and an interrupted run resumes.

Outputs (data/stage5/):
  nppes_npi.parquet        one row per NPI: type, name, last_updated, taxonomies (primary first)
  nppes_addresses.parquet  one row per NPI x address (purpose LOCATION / PRACTICE / MAILING)
  dac_addresses.parquet    one row per DAC record for our NPIs
  fetch_report.txt

Usage (from project root):
  python -m src.stage5_fetch_sources
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
import requests

from src.common.normalize import load_config

UA = {"User-Agent": "Mozilla/5.0 (HiLabs-PTD-hackathon pipeline)"}


def get_json(url, params, timeout, tries=4):
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** i)
                continue
            r.raise_for_status()
        except (requests.ConnectionError, requests.Timeout, ValueError):
            time.sleep(2 ** i)
    raise RuntimeError(f"failed after {tries} tries: {url} {params}")


def post_json(url, body, timeout, tries=5):
    for i in range(tries):
        try:
            r = requests.post(url, json=body, headers=UA, timeout=timeout, allow_redirects=False)
            if r.status_code == 200:
                return r.json()
        except (requests.ConnectionError, requests.Timeout, ValueError):
            pass
        time.sleep(2 ** i)
    raise RuntimeError(f"POST failed after {tries} tries: {url}")


# ---------------------------------------------------------------- NPPES
def fetch_nppes(npis, cfg):
    cache = cfg["paths"]["cache"] / "nppes"
    cache.mkdir(parents=True, exist_ok=True)
    todo = [n for n in npis if not (cache / f"{n}.json").exists()]
    pause, url = cfg["http_pool"]["pause_s"], cfg["sources"]["nppes_api"]

    def one(n):
        j = get_json(url, {"version": "2.1", "number": n}, cfg["http"]["timeout_s"])
        (cache / f"{n}.json").write_text(json.dumps(j), encoding="utf-8")
        time.sleep(pause)
        return n

    print(f"NPPES: {len(npis):,} NPIs, {len(npis) - len(todo):,} cached, fetching {len(todo):,}")
    with ThreadPoolExecutor(cfg["http_pool"]["workers"]) as ex:
        for i, fut in enumerate(as_completed([ex.submit(one, n) for n in todo]), 1):
            fut.result()  # re-raise any worker error
            if i % 500 == 0:
                print(f"  {i:,}/{len(todo):,}")

    npi_rows, addr_rows = [], []
    for n in npis:
        res = json.loads((cache / f"{n}.json").read_text(encoding="utf-8")).get("results") or []
        if not res:
            npi_rows.append({"npi": n, "found": False})
            continue
        r = res[0]
        b = r.get("basic", {})
        tx = sorted(r.get("taxonomies", []), key=lambda t: not t.get("primary"))
        name = b.get("organization_name") or " ".join(x for x in [b.get("first_name"), b.get("last_name")] if x)
        npi_rows.append({
            "npi": n, "found": True, "enumeration_type": r.get("enumeration_type"),
            "name": name, "credential": b.get("credential"), "status": b.get("status"),
            "last_updated": b.get("last_updated"), "enumeration_date": b.get("enumeration_date"),
            "taxonomy_codes": "|".join(t["code"] for t in tx),
            "primary_taxonomy": next((t["code"] for t in tx if t.get("primary")), tx[0]["code"] if tx else None),
            "primary_taxonomy_desc": next((t.get("desc") for t in tx if t.get("primary")), None),
            "taxonomy_states": "|".join(sorted({t.get("state") or "" for t in tx} - {""})),
        })
        for a in r.get("addresses", []):
            addr_rows.append({"npi": n, "purpose": a.get("address_purpose"), "address_1": a.get("address_1"),
                              "address_2": a.get("address_2"), "city": a.get("city"), "state": a.get("state"),
                              "zip": (a.get("postal_code") or "")[:5], "country": a.get("country_code"),
                              "phone": a.get("telephone_number")})
        for a in r.get("practiceLocations", []) or []:
            addr_rows.append({"npi": n, "purpose": "PRACTICE", "address_1": a.get("address_1"),
                              "address_2": a.get("address_2"), "city": a.get("city"), "state": a.get("state"),
                              "zip": (a.get("postal_code") or "")[:5], "country": a.get("country_code"),
                              "phone": a.get("telephone_number")})
        # Direct/FHIR endpoint "affiliation" addresses: entered separately from the practice location
        # (often by the EHR/HIE that set up the endpoint), so they can surface a current site (logic D5.9)
        for a in r.get("endpoints", []) or []:
            if a.get("address_1"):
                addr_rows.append({"npi": n, "purpose": "ENDPOINT", "address_1": a.get("address_1"),
                                  "address_2": a.get("address_2"), "city": a.get("city"), "state": a.get("state"),
                                  "zip": (a.get("postal_code") or "")[:5], "country": a.get("country_code"),
                                  "phone": None})
    return pd.DataFrame(npi_rows), pd.DataFrame(addr_rows)


# ---------------------------------------------------------------- DAC
def fetch_dac(npis, cfg):
    cache = cfg["paths"]["cache"] / "dac"
    cache.mkdir(parents=True, exist_ok=True)
    size, url = cfg["http_pool"]["dac_batch"], cfg["sources"]["dac_query"]
    batches = [npis[i:i + size] for i in range(0, len(npis), size)]

    def one(i, batch):
        f = cache / f"batch_{size}_{i:04d}.json"
        if f.exists():
            return i
        rows, offset = [], 0
        while True:  # paginate: one NPI can have many DAC rows
            # POST with a JSON body: long GET query strings get redirected (307) and refused (403)
            body = {"conditions": [{"property": "npi", "value": list(batch), "operator": "in"}],
                    "limit": 1500, "offset": offset}
            j = post_json(url, body, 120)
            got = j.get("results") or []
            rows += got
            if len(got) < 1500:
                break
            offset += 1500
        f.write_text(json.dumps({"npis": batch, "rows": rows}), encoding="utf-8")
        return i

    print(f"DAC: {len(npis):,} NPIs in {len(batches)} batches")
    with ThreadPoolExecutor(min(4, cfg["http_pool"]["workers"])) as ex:
        for fut in as_completed([ex.submit(one, i, b) for i, b in enumerate(batches)]):
            fut.result()  # re-raise any worker error instead of failing silently
    rows = []
    for i in range(len(batches)):
        rows += json.loads((cache / f"batch_{size}_{i:04d}.json").read_text(encoding="utf-8"))["rows"]
    d = pd.DataFrame(rows)
    if not d.empty:
        d["zip"] = d.zip_code.str[:5]
    meta = get_json(cfg["sources"]["dac_meta"], {}, 60)
    return d, meta.get("modified")


# ---------------------------------------------------------------- CMS facility registries (organizations)
def _paged_post(url, conditions, cache_file):
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    rows, offset = [], 0
    while True:
        j = post_json(url, {"conditions": conditions, "limit": 1500, "offset": offset}, 120)
        got = j.get("results") or []
        rows += got
        if len(got) < 1500:
            break
        offset += 1500
    cache_file.write_text(json.dumps(rows), encoding="utf-8")
    return rows


def fetch_registries(cfg):
    """In-state rows of each CMS facility registry in config sources.facility_registries."""
    cache = cfg["paths"]["cache"] / "registries"
    cache.mkdir(parents=True, exist_ok=True)
    st = cfg["scope"]["state"]
    out = []
    for reg in cfg["sources"]["facility_registries"]:
        url = cfg["sources"]["provider_data_query"].format(id=reg["id"])
        rows = _paged_post(url, [{"property": reg["state"], "value": st, "operator": "="}],
                           cache / f"{reg['id']}_{st}.json")
        for r in rows:
            out.append({"registry": reg["label"], "reg_name": r.get(reg["name"]),
                        "reg_alt_name": r.get(reg.get("alt_name", ""), None) if reg.get("alt_name") else None,
                        "reg_npi": r.get(reg["npi"]) if reg.get("npi") else None,
                        "reg_address": r.get(reg["address"]) if reg.get("address") else None,
                        "reg_city": r.get(reg["city"]), "reg_state": r.get(reg["state"]),
                        "reg_zip": (r.get(reg["zip"]) or "")[:5]})
        print(f"registry {reg['label']}: {len(rows):,} {st} rows")
    return pd.DataFrame(out)


def fetch_dac_groups(names, cfg):
    """DAC rows whose group (facility_name) exactly equals one of our organization names, in-state."""
    cache = cfg["paths"]["cache"] / "dac_groups"
    cache.mkdir(parents=True, exist_ok=True)
    url, st, size = cfg["sources"]["dac_query"], cfg["scope"]["state"], 50
    names = sorted(names)
    batches = [names[i:i + size] for i in range(0, len(names), size)]

    def one(i, b):
        import hashlib
        h = hashlib.sha1("\n".join(b).encode()).hexdigest()[:12]
        return _paged_post(url, [{"property": "facility_name", "value": b, "operator": "in"},
                                 {"property": "state", "value": st, "operator": "="}], cache / f"g_{h}.json")

    rows = []
    with ThreadPoolExecutor(4) as ex:
        for fut in as_completed([ex.submit(one, i, b) for i, b in enumerate(batches)]):
            rows += fut.result()
    d = pd.DataFrame(rows)
    if not d.empty:
        d["zip"] = d.zip_code.str[:5]
    print(f"DAC group lookup: {len(names):,} names -> {len(d):,} rows, {d.facility_name.nunique() if len(d) else 0} groups")
    return d


def main():
    cfg = load_config()
    out = cfg["paths"]["stage5"]
    out.mkdir(parents=True, exist_ok=True)
    pairs = pd.read_parquet(cfg["paths"]["stage4"] / "tin_npi_pairs.parquet")
    npis = sorted(pairs.npi.dropna().unique())

    t0 = time.time()
    npi_df, addr_df = fetch_nppes(npis, cfg)
    dac_df, dac_modified = fetch_dac(npis, cfg)
    reg_df = fetch_registries(cfg)
    from src.stage4_tin_npi_pairs import business_label
    org_names = set(npi_df[npi_df.enumeration_type == "NPI-2"].name.dropna().str.upper().str.strip())
    org_names |= {b.upper().strip() for v in pairs.business_names.dropna() for b in v.split(" | ")
                  if b and not business_label(b)}
    grp_df = fetch_dac_groups({n for n in org_names if len(n) > 3}, cfg)
    reg_df.to_parquet(out / "registry_facilities.parquet", index=False)
    grp_df.to_parquet(out / "dac_group_addresses.parquet", index=False)
    npi_df.to_parquet(out / "nppes_npi.parquet", index=False)
    addr_df.to_parquet(out / "nppes_addresses.parquet", index=False)
    dac_df.to_parquet(out / "dac_addresses.parquet", index=False)

    L = [f"C5 fetch report ({datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC, {time.time() - t0:.0f}s)",
         f"NPIs requested: {len(npis):,}",
         f"NPPES found: {int(npi_df.found.sum()):,}  types: {npi_df.enumeration_type.value_counts().to_dict()}",
         f"NPPES addresses by purpose: {addr_df.purpose.value_counts().to_dict()}",
         f"NPPES LOCATION state CA: {int(((addr_df.purpose == 'LOCATION') & (addr_df.state == 'CA')).sum()):,}",
         f"NPPES last_updated year: {npi_df.last_updated.str[:4].value_counts().sort_index().to_dict()}",
         f"DAC dataset modified: {dac_modified}",
         f"DAC rows: {len(dac_df):,}  NPIs with DAC rows: {dac_df.npi.nunique() if len(dac_df) else 0:,}",
         f"CMS registries ({cfg['scope']['state']}): {reg_df.registry.value_counts().to_dict()}",
         f"DAC group rows (exact group-name match): {len(grp_df):,}  groups: "
         f"{grp_df.facility_name.nunique() if len(grp_df) else 0:,}"]
    (out / "fetch_report.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
