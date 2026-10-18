"""C2, rebuilt from evidence: which plans use the PPO network, predicted WITHOUT the network word in the plan name.

Why a new method. All 1,312 CA plans point to the same rate file and the index carries no network field, so a plan's
network can't be read from the PTD files. Reading "PPO" / "HMO" in a plan name is the payer's own label, not proof.
This module predicts each plan's product and network use from facts that do NOT depend on that word, and only then
compares the prediction with the name.

Evidence (all public, all re-downloaded by this module, sources in config evidence.*):
  D  Plan design      The numbers in a plan name are its benefit design (copay / deductible / coinsurance / out-of-
                      pocket maximum). Health Net's broker library publishes the official 2026 plan document of every
                      large-group design. Each document states, in its own text:
                        - which contract it belongs to: "Certificate of Insurance" (insurance policy) or "Evidence of
                          Coverage" (Knox-Keene health care service plan contract, the HMO-type regime regulated by the
                          CA Department of Managed Health Care),
                        - whether it has OUT-OF-NETWORK benefits (a PPO hallmark),
                        - whether it has HMO / PPO tiers (EOA).
                      A plan whose design exists only as PPO documents is a PPO-network plan, whatever its name says.
  S  SBC              Small-group plans: the plan's federally required Summary of Benefits and Coverage states
                      "Plan Type: PPO" or "Plan Type: HMO".
  A  HSA              HSA-compatible designs exist only in Health Net's PPO catalog.
  N  Network brand    SmartCare / ExcelCare / WholeCare / Salud are tailored networks that appear only on HMO / EOA
                      plan documents. (Name codes SC / EC / SAL are read as these brands: inferred.)
  M  Max-OOP design   A design at the legal out-of-pocket maximum of an earlier year (e.g. $9,450 = 2024 federal
                      maximum) is matched on copay / deductible / coinsurance only.

Network-use categories (what the PS asks: which plans make up the PPO network):
  PPO_NETWORK_PRIMARY     PPO / HSA-PPO: in- and out-of-network benefits, Certificate of Insurance
  PPO_NETWORK_TIER        EOA (HMO tier + PPO tier for office-based professional services) and POS (HMO tier + PPO
                          tier + out-of-network): Evidence of Coverage plans that also use the PPO network
  HMO_NETWORK_ONLY        HMO designs: Evidence of Coverage, no out-of-network benefits
  EPO_NETWORK             EPO: separate PureCare One EPO network (CA Dept. of Insurance filing)
  UNDETERMINED            no evidence independent of the name

Outputs (data/stage2/): plan_design_catalog.parquet, plan_network_prediction.parquet (+ .csv), c2_plan_design.txt
Usage: python -m src.stage2_plan_design_evidence
"""
from __future__ import annotations

import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

from src.common.normalize import load_config, save_csv

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0"}
PROD_RX = re.compile(r"^(?P<prod>(?:Full Network|ExcelCare|SmartCare|Salud HMO y M.s)(?: HMO| EOA)?"
                     r"(?: Facility Deductible)?|PPO HSA|PPO|Salud Mexico)-(?P<design>[0-9a-z/%. ]+?)\s*(?:[IF]\s*)?"
                     r"\(\$(?P<oop>[\d,]+)")


# ------------------------------------------------------------------ download + parse Health Net documents
def fetch(url: str, dest: Path) -> Path:
    if not dest.exists() or dest.stat().st_size < 1000:
        r = requests.get(url, headers=UA, timeout=120)
        r.raise_for_status()
        dest.write_bytes(r.content)
    return dest


def doc_list(cfg) -> list[dict]:
    d = cfg["paths"]["healthnet_docs"]
    d.mkdir(parents=True, exist_ok=True)
    docs = []
    for market, url in cfg["evidence"]["healthnet_broker_pages"].items():
        t = fetch(url, d / f"{market}-group.html").read_text(encoding="utf-8", errors="ignore")
        for u, x in re.findall(r'<a[^>]+href="([^"]+\.pdf)"[^>]*>(.*?)</a>', t, flags=re.S | re.I):
            x = re.sub(r"<[^>]+>|\s+", " ", html.unescape(x)).replace("–", "-").replace("�", "-").strip()
            if market == "large" and "/fb/2026/" in u and PROD_RX.match(x):
                docs.append({"market": market, "title": x, "url": u})
            if market == "small" and x.startswith("SBC - ") and ("ppo" in u or "hmo" in u):
                docs.append({"market": market, "title": x, "url": u})
    seen = set()
    return [x for x in docs if not (x["url"] in seen or seen.add(x["url"]))]


def parse_doc(path: Path) -> dict:
    from pypdf import PdfReader
    r = PdfReader(str(path))
    t = re.sub(r"\s+", " ", " ".join((p.extract_text() or "") for p in r.pages[:3]))
    pt = re.search(r"Plan Type:\s*([A-Z/]+)", t)
    # the plan's own contract: "Please refer to the <X> for all terms and conditions of coverage"
    cf = re.search(r"refer to the (Certificate of Insurance|Evidence of Coverage) for all terms", t)
    offered = re.search(r"((?:Salud con Health Net )?(?:HMO|EOA|PPO)[^.]{0,40}?) (?:is|are|plans are) offered by "
                        r"(Health Net of California, Inc\.|Health Net Life Insurance Company)", t)
    return {"contract_form": cf.group(1) if cf else None,
            # a real out-of-network BENEFIT: the benefit table has an IN-NETWORK / OUT-OF-NETWORK column pair
            # (mentions such as "out-of-network providers ... are not covered" do not count)
            "has_out_of_network": bool(re.search(r"IN-NETWORK\s+OUT-OF-\s?NETWORK", t)),
            "hmo_ppo_tiers": "HMO:" in t and "PPO:" in t,
            "offered_by": f"{offered.group(1).strip()} -> {offered.group(2)}" if offered else None,
            "sbc_plan_type": pt.group(1) if pt else None}


def design_key(design: str, oop: int) -> str:
    toks = design.replace(" ", "").split("/")
    ded, coins = "", ""
    for tk in toks[1:]:
        if tk.endswith("%"):
            coins = tk[:-1]
        else:
            ded = tk
    return f"{toks[0]}|{ded}|{coins}|{oop}"


def build_catalog(cfg) -> pd.DataFrame:
    d = cfg["paths"]["healthnet_docs"] / "docs"
    d.mkdir(parents=True, exist_ok=True)
    docs = doc_list(cfg)
    base = cfg["evidence"]["healthnet_doc_base"]

    def one(x):
        p = fetch(base + x["url"] if x["url"].startswith("/") else x["url"], d / x["url"].rsplit("/", 1)[-1])
        return {**x, **parse_doc(p)}
    with ThreadPoolExecutor(8) as ex:
        rows = list(ex.map(one, docs))
    cat = pd.DataFrame(rows)
    m = cat.title.str.extract(PROD_RX)
    cat["product"] = m["prod"]
    cat["design"] = m["design"].str.strip()
    cat["oop"] = pd.to_numeric(m["oop"].str.replace(",", ""), errors="coerce")
    cat["design_key"] = [design_key(a, int(b)) if isinstance(a, str) else None for a, b in zip(cat.design, cat.oop)]
    cat["sbc_name"] = cat.title.str.replace(r"^SBC - | \(PDF\)$", "", regex=True).str.replace(" + Infertility", " INF")

    def family(r):
        if r.market == "small":
            return {"PPO": "PPO", "HMO": "HMO"}.get(r.sbc_plan_type)
        p = r["product"] or ""
        return "PPO" if p.startswith("PPO") else ("EOA" if "EOA" in p else ("HMO" if p else None))
    cat["family"] = [family(r) for _, r in cat.iterrows()]
    cat["hsa"] = cat["product"].fillna("").str.contains("HSA")
    cat["brand"] = cat["product"].fillna(cat.title).str.extract(r"(SmartCare|ExcelCare|WholeCare|Salud|Full Network)")[0]
    return cat


def readable(k: str) -> str:
    """'30|500|30|4000' -> 'copay $30 / deductible $500 / coinsurance 30% / out-of-pocket max $4,000'"""
    cop, ded, coins, oop = (k.split("|") + ["", "", "", ""])[:4]
    if not ded and coins and cop.isdigit() and int(cop) >= 500:
        # HSA-compatible designs are written deductible/coinsurance ("PPO HSA-4000/0%"): first number = deductible
        return " / ".join(p for p in (f"deductible ${int(cop):,}", f"coinsurance {coins}%",
                                      f"out-of-pocket max ${int(oop):,}" if oop else "") if p)
    parts = [f"copay ${cop}"]
    if ded:
        kind = {"a": " (applies to selected services)", "d": " (facility)"}.get(ded[-1], "")
        parts.append(f"deductible ${int(ded.rstrip('ad')):,}{kind}")
    if coins:
        parts.append(f"coinsurance {coins}%")
    if oop:
        parts.append(f"out-of-pocket max ${int(oop):,}")
    return " / ".join(parts)


# ------------------------------------------------------------------ index plans -> design
def plan_design_key(name: str):
    ms = re.findall(r"(?<![\w$])(\d+[ad]?%?(?:/\d+[ad]?%?){1,3})(?![\w])", name)
    if not ms:
        return None
    coins, nums = "", []
    for p in ms[-1].split("/"):
        if p.endswith("%"):
            coins = p[:-1]
        else:
            nums.append(p)
    if len(nums) < 2 or not nums[-1].isdigit():
        return None
    return f"{nums[0]}|{nums[1] if len(nums) == 3 else ''}|{coins}|{int(nums[-1])}"


def main():
    cfg = load_config()
    P = cfg["paths"]
    cat = build_catalog(cfg)
    BRANDS = cfg["evidence"]["tailored_network_codes"]
    oop_floor = cfg["evidence"]["legal_max_oop_floor"]
    cat.to_parquet(P["stage2"] / "plan_design_catalog.parquet", index=False)
    large = cat[cat.market == "large"].dropna(subset=["design_key"])

    # facts per design key (large group)
    by_key = large.groupby("design_key").agg(
        families=("family", lambda s: "|".join(sorted(set(s)))),
        contract_forms=("contract_form", lambda s: "|".join(sorted({x for x in s if x}))),
        out_of_network=("has_out_of_network", "any"), docs=("title", lambda s: " ; ".join(sorted(s)[:3])))
    large["key3"] = large.design_key.str.rsplit("|", n=1).str[0]
    by_key3 = large[large.oop >= oop_floor].groupby("key3").family.agg(lambda s: "|".join(sorted(set(s))))
    any_key3 = large.groupby("key3").family.agg(lambda s: "|".join(sorted(set(s))))   # copay/ded/coins, any OOP
    sbc = (cat[(cat.market == "small") & cat.family.notna()].drop_duplicates("sbc_name")
           .set_index("sbc_name")[["family", "contract_form"]])
    brand_fams = cat.dropna(subset=["brand", "family"]).groupby("brand").family.agg(lambda s: "|".join(sorted(set(s))))
    hsa_fams = "|".join(sorted(set(large[large.hsa].family)))

    lab = pd.read_parquet(P["stage2"] / "plan_labels.parquet")
    rows = []
    for r in lab.itertuples():
        name = r.plan_name
        k = plan_design_key(name)
        ev, fam = [], None
        partial = re.findall(r"(?<![\w$])(\d+/\d+[ad]?/\d+%)(?![\w/])", name)   # e.g. "CUS PPO 15/250/20%"
        if name in sbc.index:                                         # S: the plan's own SBC
            fam = sbc.loc[name, "family"]
            ev.append(f"S: Summary of Benefits and Coverage for this exact plan says Plan Type {fam}")
        elif k and k in by_key.index:                                 # D: exact design
            fam = by_key.loc[k, "families"]
            ev.append(f"D: benefit design ({readable(k)}) exists in Health Net's 2026 plan catalog only as {fam} "
                      f"(its plan document points to the {by_key.loc[k, 'contract_forms'].replace('|', ' / ')}; "
                      f"out-of-network benefits: {'yes' if by_key.loc[k, 'out_of_network'] else 'no'})")
        elif k and k.rsplit("|", 1)[0] in by_key3.index and int(k.rsplit("|", 1)[1]) >= oop_floor:   # M
            fam = by_key3.loc[k.rsplit("|", 1)[0]]
            ev.append(f"M: benefit design ({readable(k)}) = a 2026 {fam} design with the out-of-pocket max at an "
                      f"earlier year's legal maximum")
        elif partial and (k3 := "|".join(partial[-1].replace("%", "").split("/"))) in any_key3.index \
                and "|" not in any_key3.loc[k3]:                     # M2: partial design, one family only
            fam = any_key3.loc[k3]
            ev.append(f"M2: copay/deductible/coinsurance {partial[-1]} exists in Health Net's 2026 catalog only as {fam}")
        elif re.search(r"\bHSA[FI]?\b", name):                        # A (HSA, HSAF = family, HSAI = individual)
            fam = hsa_fams
            ev.append(f"A: HSA-compatible design; Health Net offers HSA-compatible plans only as {hsa_fams}")
        else:                                                         # N
            code = next((c for c in BRANDS if re.search(rf"\b{c}\b", name)), None)
            brand = BRANDS.get(code) or next((b for b in BRANDS.values() if b.lower() in name.lower()), None)
            if brand and brand in brand_fams.index:
                fam = brand_fams.loc[brand]
                ev.append(f"N: tailored network {brand}{' (code ' + code + ', inferred)' if code else ''} appears "
                          f"only on {fam} plan documents")
        rows.append({"plan_row_id": r.plan_row_id, "plan_name": name, "plan_id": r.plan_id, "design_key": k,
                     "evidence_family": fam, "evidence": " | ".join(ev), "name_label": r.product_type})
    pr = pd.DataFrame(rows)

    def category(fam, name_label):
        if fam == "PPO":
            return "PPO_NETWORK_PRIMARY"
        if fam in ("HMO", "EOA|HMO", "EOA"):
            # design/brand evidence = HMO-family benefit grid; EOA vs HMO differ only in the PPO tier, which the design
            # grid doesn't show; the EOA word (when present) adds the tier
            return "PPO_NETWORK_TIER" if name_label == "EOA_HYBRID" else "HMO_NETWORK_ONLY"
        return "UNDETERMINED"
    pr["network_use"] = [category(f, n) for f, n in zip(pr.evidence_family, pr.name_label)]
    # POS / EPO: documented product definitions (no catalog designs): POS flyer FLY032979EP01, CDI EPO filing
    pr.loc[pr.name_label.eq("POS") & pr.network_use.eq("UNDETERMINED"), "network_use"] = "PPO_NETWORK_TIER"
    pr.loc[pr.name_label.eq("POS"), "evidence"] += " | POS: " + cfg["evidence"]["product_documents"]["POS"]["text"]
    pr.loc[pr.name_label.eq("EPO"), "network_use"] = "EPO_NETWORK"
    pr["evidence_based"] = pr.evidence_family.notna()
    name_ppo = pr.name_label.eq("PPO")
    pr["agrees_with_name"] = (pr.network_use.eq("PPO_NETWORK_PRIMARY") == name_ppo) | ~pr.evidence_based
    # Final answer: evidence first. Plans with no public design or document (employer-custom designs, exchange
    # products) fall back to the network word in their name and are marked NAME_ONLY (low confidence).
    from_name = {"PPO": "PPO_NETWORK_PRIMARY", "HMO": "HMO_NETWORK_ONLY", "EOA_HYBRID": "PPO_NETWORK_TIER",
                 "POS": "PPO_NETWORK_TIER", "EPO": "EPO_NETWORK"}
    und = pr.network_use.eq("UNDETERMINED")
    pr["final_network_use"] = pr.network_use.where(~und, pr.name_label.map(from_name))
    pr["basis"] = "EVIDENCE"
    pr.loc[pr.name_label.isin(["POS", "EPO"]), "basis"] = "PRODUCT_DOCUMENT"
    pr.loc[und, "basis"] = "NAME_ONLY"
    pr["uses_ppo_network"] = pr.final_network_use.isin(["PPO_NETWORK_PRIMARY", "PPO_NETWORK_TIER"])
    pr.to_parquet(P["stage2"] / "plan_network_prediction.parquet", index=False)
    save_csv(pr, P["stage2"] / "plan_network_prediction.csv")

    ev_only = pr[pr.evidence_based]
    L = ["C2 rebuilt: plan network predicted from plan design, plan documents and network brands (not the network word)",
         "=" * 104,
         f"Health Net 2026 documents parsed: {len(cat)} (large-group plan overviews {int((cat.market == 'large').sum())}, "
         f"small-group SBCs {int((cat.market == 'small').sum())})",
         "Large-group design catalog: " + ", ".join(f"{k} {v}" for k, v in by_key.families.value_counts().items())
         + " design(s); designs shared by PPO and HMO/EOA: "
         + str(int(by_key.families.str.contains("PPO").sum() - (by_key.families == "PPO").sum())),
         "Contract form by family (2026 plan overviews): "
         + str(large.groupby("family").contract_form.agg(lambda s: s.value_counts().to_dict()).to_dict()),
         "Out-of-network benefit column by family: " + str(large.groupby("family").has_out_of_network.mean().round(2).to_dict()),
         "HMO / PPO tier columns by family: " + str(large.groupby("family").hmo_ppo_tiers.mean().round(2).to_dict()),
         "'... offered by' statements: " + str(large.offered_by.value_counts().head(6).to_dict()),
         "", "Evidence used per plan:",
         pr.evidence.str[:2].replace("", "none").value_counts().to_string(),
         "", "Network use (prediction):", pr.network_use.value_counts().to_string(),
         "", "Prediction vs the plan-name word (only plans with independent evidence):",
         pd.crosstab(ev_only.network_use, ev_only.name_label).to_string(),
         f"\nPlans predicted PPO_NETWORK_PRIMARY from evidence: {int((ev_only.network_use == 'PPO_NETWORK_PRIMARY').sum())}; "
         f"named PPO among them: {int((ev_only.network_use.eq('PPO_NETWORK_PRIMARY') & ev_only.name_label.eq('PPO')).sum())}",
         f"Plans with evidence whose prediction disagrees with the name: {int((~ev_only.agrees_with_name).sum())}",
         f"Plans with no independent evidence (custom designs / exchange plans): {int((~pr.evidence_based).sum())}",
         "", "FINAL network use (evidence first; NAME_ONLY = no public design/document, name word used):",
         pd.crosstab(pr.final_network_use, pr.basis, margins=True).to_string(),
         f"\nPlans whose members use the PPO network: {int(pr.uses_ppo_network.sum())} "
         f"(primary {int(pr.final_network_use.eq('PPO_NETWORK_PRIMARY').sum())}, "
         f"as a tier {int(pr.final_network_use.eq('PPO_NETWORK_TIER').sum())})"]
    (P["stage2"] / "c2_plan_design.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
