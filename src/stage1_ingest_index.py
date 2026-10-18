"""C1: parse Centene Table-of-Contents (index) files into plan / file tables.

Outputs (data/stage1/):
  c1_plan_file_table.parquet / .csv   THE C1 table: one row per unique (plan, rate file)
                                      brand, plan name, plan id, market type, rate file URL, scope
  plans.parquet          one row per (index file, reporting_structure, plan) -- nothing dropped
  files.parquet          one row per unique rate file (dedup key = sha1 of normalized URL)
  plan_file_map.parquet  plan_row_id <-> file_key links
  index_files.parquet    provenance of every index file (URL, sha256, header fields)
  c1_report.txt          human-readable counts

Usage (from project root):
  python -m src.stage1_ingest_index --only healthnet
  python -m src.stage1_ingest_index            # all index files in config.yaml
  python -m src.stage1_ingest_index --sample   # synthetic tests/sample_index.json
"""
from __future__ import annotations

import argparse
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import ijson
import pandas as pd
import requests

from src.common.normalize import ROOT, file_name_from_url, load_config, norm_url, save_csv

HEADER_KEYS = {"reporting_entity_name", "reporting_entity_type", "last_updated_on", "version"}

RE_EIN = re.compile(r"^\d{9}$")
RE_HIOS = re.compile(r"^\d{5}[A-Z]{2}\d{3}(\d{4}(-?\d{2})?)?$")  # issuer(5)+state(2)+product(3)[+plan(4)[+variant]]
RE_HIOS_ISSUER = re.compile(r"^\d{5}$")  # HIOS issuer id alone (seen in QualChoice)
RE_PLACEHOLDER = re.compile(r"^(\d)\1+$")  # 999999999, 000000000, ...
RE_CA_NAME = re.compile(r"^\s*CA\b")  # Health Net names start "CA L ..."
# 2-letter token in Centene rate-file names: "..._healthnet-ca_in-network.json" -> "ca", "fidelis-ex" -> "ex"
RE_FILE_TOKEN = re.compile(r"-([a-z]{2})_(?:in-network|allowed-amounts)", re.I)
US_STATES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY "
    "NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR".split())


# ---------------------------------------------------------------- download

def download(url: str, dest: Path, cfg: dict, refresh: bool = False) -> Path:
    """Download url -> dest (cached). Writes to .part then renames, so a failed run leaves no half file."""
    if dest.exists() and not refresh:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    headers = {"User-Agent": cfg["http"]["user_agent"]}
    with requests.get(url, headers=headers, stream=True, timeout=cfg["http"]["timeout_s"]) as r:
        if r.status_code == 403:
            raise SystemExit(f"403 Forbidden for {url}. Use a VPN, or save it from a browser to {dest}")
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    tmp.replace(dest)
    return dest


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def short_hash(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- classify helpers

def plan_id_format(pid: str | None) -> str:
    if not pid:
        return "MISSING"
    if RE_PLACEHOLDER.match(pid):
        return "PLACEHOLDER"
    if RE_EIN.match(pid):
        return "EIN"
    if RE_HIOS.match(pid):
        return "HIOS"
    if RE_HIOS_ISSUER.match(pid):
        return "HIOS_ISSUER"
    return "OTHER"


def norm_plan_name(name: str) -> str:
    """Upper-case, single spaces, no spaces around - and / ("COFS - $1250" == "COFS -$1250")."""
    s = re.sub(r"\s+", " ", (name or "").upper()).strip()
    return re.sub(r"\s*([-/])\s*", r"\1", s)


def file_tokens(urls) -> tuple[set[str], set[str]]:
    """(US-state tokens, other segment tokens) found in the rate-file names of one reporting_structure."""
    toks = {m.group(1).upper() for u in urls if u for m in [RE_FILE_TOKEN.search(file_name_from_url(u))] if m}
    return toks & US_STATES, toks - US_STATES


def ca_evidence(plan_name: str, pid: str, fmt: str, states: set[str], want: str = "CA") -> str:
    """List every independent reason to believe a plan is Californian ('' if none).

    Brand alone is NOT evidence: Health Net also files Oregon plans in the same index.
    """
    ev = []
    if want in states:
        ev.append(f"file_state:{want}")
    if RE_CA_NAME.match(plan_name or ""):
        ev.append("name_prefix:CA")
    if fmt == "HIOS" and pid[5:7] == "CA":
        ev.append("hios_state:CA")
    return "; ".join(ev)


# ---------------------------------------------------------------- parsing

def read_header(path: Path) -> dict:
    """Top-level scalar fields, wherever they appear in the file (streamed)."""
    out = {}
    with open(path, "rb") as f:
        for prefix, event, value in ijson.parse(f):
            if prefix in HEADER_KEYS and event in ("string", "number"):
                out[prefix] = str(value)
    return out


def parse_index(path: Path, src: dict, cfg: dict):
    """Return (plan_rows, link_rows) built from one index file, streaming reporting_structure items."""
    brand = src["brand"]
    bcfg = cfg["scope"]["brands"].get(brand, {"in_scope": False, "reason": "brand not in config"})
    want_market = cfg["scope"]["market_type"]
    want_state = cfg["scope"]["state"].upper()
    plans, links = [], []
    with open(path, "rb") as f:
        for s_idx, rs in enumerate(ijson.items(f, "reporting_structure.item")):
            structure_id = f"{src['name']}:{s_idx}"
            rfiles = []
            for fi in rs.get("in_network_files") or []:
                rfiles.append(("in_network", fi.get("location"), fi.get("description")))
            aa = rs.get("allowed_amount_file")
            if aa:
                rfiles.append(("allowed_amount", aa.get("location"), aa.get("description")))
            states, segments = file_tokens(loc for _, loc, _ in rfiles)
            other_states = sorted(states - {want_state})
            for p_idx, p in enumerate(rs.get("reporting_plans") or []):
                name = (p.get("plan_name") or "").strip()
                pid = str(p.get("plan_id") or "").strip()
                fmt = plan_id_format(pid)
                id_type = (p.get("plan_id_type") or "").strip().lower()
                mkt = (p.get("plan_market_type") or "").strip().lower()
                ev = ca_evidence(name, pid, fmt, states, want_state)
                reasons = []
                if not bcfg["in_scope"]:
                    reasons.append(f"brand_out:{bcfg['reason']}")
                if mkt != want_market:
                    reasons.append(f"market_type={mkt or 'missing'}")
                if other_states:
                    reasons.append(f"file_state={','.join(other_states)}")
                if not ev:
                    reasons.append(f"no_{want_state}_evidence")
                plan_row_id = f"{structure_id}:{p_idx}"
                plans.append({
                    "plan_row_id": plan_row_id,
                    "brand": brand,
                    "index_name": src["name"],
                    "structure_id": structure_id,
                    "plan_name": name,
                    "plan_name_norm": norm_plan_name(name),
                    "plan_id": pid,
                    "plan_id_type": id_type,
                    "plan_id_format": fmt,
                    "id_type_mismatch": id_type == "ein" and fmt in ("HIOS", "HIOS_ISSUER"),
                    "plan_market_type": mkt,
                    "issuer": p.get("issuer_name"),
                    "sponsor": p.get("plan_sponsor_name"),
                    "ca_evidence": ev,
                    "file_states": ",".join(sorted(states)),
                    "file_segments": ",".join(sorted(segments)),
                    "in_scope": not reasons,
                    "scope_reason": "; ".join(reasons) if reasons else "in_scope",
                    "n_files_in_structure": len(rfiles),
                })
                for role, loc, desc in rfiles:
                    if loc:
                        links.append({"plan_row_id": plan_row_id, "structure_id": structure_id,
                                      "index_name": src["name"], "brand": brand, "file_role": role,
                                      "url": loc, "description": desc, "plan_in_scope": not reasons})
            if not rs.get("reporting_plans"):  # files with no plans: keep them visible
                for role, loc, desc in rfiles:
                    if loc:
                        links.append({"plan_row_id": None, "structure_id": structure_id,
                                      "index_name": src["name"], "brand": brand, "file_role": role,
                                      "url": loc, "description": desc, "plan_in_scope": False})
    return plans, links


def add_plan_keys(plans: pd.DataFrame) -> pd.DataFrame:
    """plan_key = one logical plan (brand + normalized name + id + market). Flags exact repeats."""
    plans = plans.copy()
    plans["plan_key"] = (plans.brand + "|" + plans.plan_name_norm + "|" + plans.plan_id + "|"
                         + plans.plan_market_type).map(short_hash)
    plans["n_rows_same_key"] = plans.groupby("plan_key").plan_row_id.transform("size")
    plans["dup_in_structure"] = plans.duplicated(["structure_id", "plan_name", "plan_id", "plan_market_type"])
    return plans


def build_files(links: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    links = links.copy()
    links["norm_url"] = links["url"].map(norm_url)
    links["file_key"] = links["norm_url"].map(short_hash)

    def join_unique(s):
        return " | ".join(sorted({str(x) for x in s if x is not None and str(x) != ""}))

    g = links.groupby("file_key")
    files = pd.DataFrame({
        "norm_url": g["norm_url"].first(),
        "example_url": g["url"].first(),
        "file_role": g["file_role"].agg(join_unique),
        "descriptions": g["description"].agg(join_unique),
        "brands": g["brand"].agg(join_unique),
        "index_names": g["index_name"].agg(join_unique),
        "n_index_files": g["index_name"].nunique(),
        "n_url_variants": g["url"].nunique(),
        "n_plan_rows": g["plan_row_id"].nunique(),
        "n_structures": g["structure_id"].nunique(),
        "n_in_scope_plans": links[links.plan_in_scope].groupby("file_key")["plan_row_id"].nunique(),
    }).reset_index()
    files["n_in_scope_plans"] = files["n_in_scope_plans"].fillna(0).astype(int)
    files["file_name"] = files["norm_url"].map(file_name_from_url)
    files["in_scope"] = files["n_in_scope_plans"] > 0
    cols = ["file_key", "file_name", "file_role", "descriptions", "brands", "index_names", "n_index_files",
            "n_plan_rows", "n_structures", "n_in_scope_plans", "in_scope", "n_url_variants",
            "norm_url", "example_url"]
    pfm = links.dropna(subset=["plan_row_id"])[["plan_row_id", "structure_id", "file_key", "file_role"]]
    return files[cols], pfm.drop_duplicates()


def build_c1_table(plans: pd.DataFrame, files: pd.DataFrame, pfm: pd.DataFrame) -> pd.DataFrame:
    """THE C1 deliverable: one row per unique (plan_key, file_key); duplicate files/plan rows collapsed."""
    j = pfm.merge(plans, on=["plan_row_id", "structure_id"]).merge(
        files[["file_key", "file_name", "example_url"]], on="file_key")

    def join_unique(s):
        return " | ".join(sorted({str(x) for x in s if x}))

    g = j.groupby(["plan_key", "file_key"])
    t = g.agg(brand=("brand", "first"), plan_name=("plan_name", "first"),
              plan_name_variants=("plan_name", join_unique), plan_id=("plan_id", "first"),
              plan_id_type=("plan_id_type", "first"), plan_id_format=("plan_id_format", "first"),
              plan_market_type=("plan_market_type", "first"), issuer=("issuer", "first"),
              file_role=("file_role", "first"), file_name=("file_name", "first"),
              rate_file_url=("example_url", "first"), in_scope=("in_scope", "max"),
              scope_reason=("scope_reason", "first"), ca_evidence=("ca_evidence", "first"),
              source_index_names=("index_name", join_unique), n_source_rows=("plan_row_id", "nunique"),
              ).reset_index()
    order = ["brand", "plan_name", "plan_id", "plan_market_type", "file_role", "rate_file_url", "file_name",
             "in_scope", "scope_reason", "ca_evidence", "plan_id_type", "plan_id_format", "issuer",
             "plan_name_variants", "source_index_names", "n_source_rows", "plan_key", "file_key"]
    return t[order].sort_values(["in_scope", "brand", "plan_name", "plan_id", "file_role"],
                                ascending=[False, True, True, True, True]).reset_index(drop=True)


# ---------------------------------------------------------------- report

def write_report(path: Path, idx: pd.DataFrame, plans: pd.DataFrame, files: pd.DataFrame,
                 table: pd.DataFrame, n_refs: int, want_state: str) -> str:
    L = []
    L.append(f"C1 report  (generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)")
    L.append("=" * 78)

    L.append(f"\n[1] Index files parsed: {len(idx)}")
    for r in idx.itertuples():
        same = f"  IDENTICAL TO {r.identical_to}" if r.identical_to else ""
        L.append(f"  {r.index_name:31s} ver={r.version} updated={r.last_updated_on} "
                 f"structures={r.n_structures:<3} plans={r.n_plan_rows:<5} bytes={r.bytes:>9,}{same}")
    L.append("  entity / type: " + "; ".join(sorted(set(idx.reporting_entity_name + " / " + idx.reporting_entity_type))))

    L.append("\n[2] Plans (rows = plan x reporting_structure; nothing dropped)")
    L.append(f"  plan rows                       : {len(plans):,}")
    L.append(f"  unique plans (plan_key)         : {plans.plan_key.nunique():,}   "
             "(plan_key = brand + normalized name + id + market type)")
    L.append(f"  distinct plan names / plan ids  : {plans.plan_name.nunique():,} / {plans.plan_id.nunique():,}")
    L.append(f"  IN-SCOPE plan rows              : {int(plans.in_scope.sum()):,}  "
             f"(unique plans {plans[plans.in_scope].plan_key.nunique():,})")
    L.append("  scope by brand (plan rows):")
    L.append(f"     {'brand':11s} {'in':>6s} {'out':>6s}  market types        states in file names / segments")
    for b, g in plans.groupby("brand"):
        st = sorted({s for v in g.file_states for s in v.split(",") if s})
        sg = sorted({s for v in g.file_segments for s in v.split(",") if s})
        mk = ", ".join(f"{k}={v}" for k, v in g.plan_market_type.value_counts().items())
        L.append(f"     {b:11s} {int(g.in_scope.sum()):6,} {int((~g.in_scope).sum()):6,}  {mk:18s}  "
                 f"{' '.join(st) or '-'}{'  / segments: ' + ' '.join(sg) if sg else ''}")
    L.append("  exclusion reasons (state codes collapsed):")
    collapsed = plans.loc[~plans.in_scope, "scope_reason"].str.replace(
        r"file_state=[A-Z,]+", "file_state=non-" + want_state, regex=True)
    for reason, n in collapsed.value_counts().items():
        L.append(f"     {n:6,}  {reason}")
    ca_out = plans[(~plans.in_scope) & (plans.ca_evidence != "")]
    L.append(f"  plans WITH {want_state} evidence but OUT of scope: {len(ca_out):,}")
    for (b, reason), g in ca_out.groupby(["brand", "scope_reason"]):
        L.append(f"     {b}: {len(g)} rows, reason={reason}; e.g. {', '.join(g.plan_name.head(3))}")
    L.append("  issuers: " + ", ".join(f"{k} ({v})" for k, v in plans.issuer.fillna("(none)").value_counts().items()))

    L.append("\n[3] Plan data quality")
    for (t, fmt), n in plans.groupby(["plan_id_type", "plan_id_format"]).size().items():
        L.append(f"     plan_id_type={t:6s} format={fmt:12s} {n:,}")
    L.append(f"  id_type 'ein' but HIOS-format id  : {int(plans.id_type_mismatch.sum()):,} "
             f"(e.g. {', '.join(plans[plans.id_type_mismatch].plan_id.unique()[:3])})")
    ph = plans[plans.plan_id_format == "PLACEHOLDER"]
    L.append(f"  placeholder ids ({', '.join(sorted(ph.plan_id.unique())) or '-'}) : {len(ph):,} rows: "
             + "; ".join(ph.plan_name.head(3)))
    names_per_id = plans.groupby("plan_id").plan_name.nunique()
    ids_per_name = plans.groupby("plan_name").plan_id.nunique()
    L.append(f"  plan ids carrying >1 plan name    : {(names_per_id > 1).sum():,} (max {names_per_id.max()})")
    L.append(f"  plan names under >1 plan id       : {(ids_per_name > 1).sum():,} (max {ids_per_name.max()})")
    L.append(f"  exact duplicate rows in a structure: {int(plans.dup_in_structure.sum()):,} "
             f"({'; '.join(plans[plans.dup_in_structure].plan_name.head(3))})")
    var = plans.groupby("plan_key").plan_name.unique()
    var = var[var.map(len) > 1]
    L.append(f"  names that differ only by spacing : {len(var):,} plan keys "
             f"(e.g. {'; '.join(' vs '.join(repr(n) for n in v) for v in var.head(3))})")

    L.append("\n[4] Rate files (dedup key = scheme+host+path; query string dropped)")
    L.append(f"  file references (one per structure) : {n_refs:,}")
    L.append(f"  unique files                        : {len(files):,}  "
             f"({', '.join(f'{k}={v}' for k, v in files.file_role.value_counts().items())})")
    L.append(f"  duplicate references removed        : {n_refs - len(files):,}")
    for r in files[files.n_structures > 1].itertuples():
        L.append(f"     {r.file_name} referenced by {r.n_structures} structures in {r.n_index_files} index files")
    L.append(f"  urls differing only by query string : {int((files.n_url_variants > 1).sum()):,}")
    L.append(f"  IN-SCOPE files                      : {int(files.in_scope.sum()):,}")
    for r in files[files.in_scope].sort_values("file_role").itertuples():
        L.append(f"     {r.file_role:14s} {r.n_in_scope_plans:5,} in-scope plans / {r.n_plan_rows:5,} total  {r.file_name}")

    L.append("\n[5] C1 table (c1_plan_file_table): one row per unique (plan, rate file)")
    L.append(f"  rows {len(table):,} | in-scope rows {int(table.in_scope.sum()):,} | "
             f"unique plans {table.plan_key.nunique():,} | unique files {table.file_key.nunique():,}")
    text = "\n".join(L) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="brand or index name(s) to process, e.g. healthnet")
    ap.add_argument("--refresh", action="store_true", help="re-download even if cached")
    ap.add_argument("--sample", action="store_true", help="run on tests/sample_index.json (as healthnet)")
    args = ap.parse_args(argv)

    cfg = load_config()
    out = cfg["paths"]["stage1"]
    if args.sample:
        sources = [{"brand": "healthnet", "name": "sample", "url": "file://tests/sample_index.json",
                    "local": ROOT / "tests" / "sample_index.json"}]
        out = out.parent / "stage1_sample"
    else:
        sources = cfg["index_files"]
        if args.only:
            sources = [s for s in sources if s["brand"] in args.only or s["name"] in args.only]
        if not sources:
            raise SystemExit(f"No index files match --only {args.only}")
    out.mkdir(parents=True, exist_ok=True)

    all_plans, all_links, idx_rows = [], [], []
    for src in sources:
        path = src.get("local") or download(src["url"], cfg["paths"]["raw_index"] / f"{src['name']}.json",
                                            cfg, args.refresh)
        print(f"parsing {src['name']} <- {path}")
        hdr = read_header(path)
        plans, links = parse_index(path, src, cfg)
        all_plans += plans
        all_links += links
        idx_rows.append({"index_name": src["name"], "brand": src["brand"], "url": src["url"],
                         "local_path": Path(path).relative_to(ROOT).as_posix(), "bytes": Path(path).stat().st_size,
                         "sha256": sha256_of(path),
                         "parsed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         "n_structures": len({p["structure_id"] for p in plans} |
                                             {l["structure_id"] for l in links}),
                         "n_plan_rows": len(plans),
                         **{k: hdr.get(k) for k in sorted(HEADER_KEYS)}})

    plans = add_plan_keys(pd.DataFrame(all_plans))
    links = pd.DataFrame(all_links)
    idx = pd.DataFrame(idx_rows)
    first_of = idx.groupby("sha256").index_name.transform("first")
    idx["identical_to"] = first_of.where(first_of != idx.index_name, "")  # byte-identical index files
    files, pfm = build_files(links)
    table = build_c1_table(plans, files, pfm)

    plans.to_parquet(out / "plans.parquet", index=False)
    files.to_parquet(out / "files.parquet", index=False)
    pfm.to_parquet(out / "plan_file_map.parquet", index=False)
    idx.to_parquet(out / "index_files.parquet", index=False)
    table.to_parquet(out / "c1_plan_file_table.parquet", index=False)
    save_csv(table, out / "c1_plan_file_table.csv")
    n_refs = links[["structure_id", "url"]].drop_duplicates().shape[0]  # file references, one per structure
    text = write_report(out / "c1_report.txt", idx, plans, files, table, n_refs, cfg["scope"]["state"].upper())
    print(text)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
