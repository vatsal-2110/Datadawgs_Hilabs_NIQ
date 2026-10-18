"""C2 Layer 1: label every in-scope plan by network type from its name (plus researched codes).

Input : data/stage1/plans.parquet (in_scope rows)
Output: data/stage2/plan_labels.parquet (+ .csv), one row per in-scope plan row:
        product_type (PPO / HMO / EPO / POS / EOA_HYBRID / UNKNOWN) = benefit rules,
        provider_network (PPO_NETWORK / HMO_NETWORK / HMO_NETWORK+PPO_SPECIALISTS / UNCONFIRMED) = which doctors,
        is_ppo, uses_ppo_network, uses_ppo_network_incl_inferred, confidence, rule, evidence, source

Rules are applied in priority order; the first that matches wins (see docs/C1_C2_C4_decision_log.md, D2.2-D2.5).

Usage (from project root):
  python -m src.stage2_plan_labels
"""
from __future__ import annotations

import re

import pandas as pd

from src.common.normalize import load_config, save_csv

# Sources for researched facts (retrieved 2026-10-17)
SRC_TYPES = "https://www.healthnet.com/en_us/find-a-plan/large-employers/type-of-plans.html"
SRC_EOA = "https://www.healthnet.com/content/dam/centene/healthnet/pdfs/broker/ca/general/EOA_flyer_eng.pdf"
SRC_CANOPY = "https://wordandbrown.com/NewsPost/Large-Group-Carrier-Spotlight-Health-Net"
# Health Net Life Insurance Co. EPO member handbook for one employer (Northrop Grumman), PDF created 2009-05-15
SRC_HN_EPO = "https://www.healthnet.com/static/custom/unprotected/pdfs/CA58857_Northrop_handbook.pdf"
# California Department of Insurance: network-adequacy waiver filing for Health Net's PureCare One EPO network
SRC_CDI_EPO = ("https://www.insurance.ca.gov/01-consumers/110-health/10-basics/upload/"
               "Network-Waiver-Health-Net-SBG-PureCare-One-EPO-PF-2018-01437-2.pdf")

# Two axes (D2.4): product_type = benefit rules; provider_network = which doctors the plan uses.
# *_INFERRED = not documented for these exact plans; reasoning + source recorded in network_evidence.
NETWORK_OF = {
    "PPO": ("PPO_NETWORK", "PPO plan uses the PPO network (Health Net plan-type page)"),
    # Updated 2026-10-18: the 2009 handbook inference is superseded by current regulator filings: Health
    # Net's EPO runs on its own PureCare One EPO network (CA Dept. of Insurance network-waiver filing, config
    # evidence.product_types.EPO). So an EPO plan is NOT evidence of PPO-network use.
    "EPO": ("EPO_NETWORK", "Health Net EPO = Health Net Life on the PureCare One EPO network, a separate network "
                           "(CA Dept. of Insurance network-waiver filing); not the PPO network"),
    "HMO": ("HMO_NETWORK", "HMO plan uses an HMO network (Full Network or tailored: SmartCare/ExcelCare/Salud/CanopyCare)"),
    "EOA_HYBRID": ("HMO_NETWORK+PPO_SPECIALISTS", "EOA: HMO network via PCP/physician group; PPO-network "
                                                  "specialists for exams/evaluations only"),
    "POS": ("HMO_NETWORK+OUT_OF_NETWORK", "POS plans are offered by Health Net of California, Inc. (DMHC), the HMO "
                                          "licensee (Health Net claim form FRM049776EC01); HMO tier + out-of-network tier"),
    "UNKNOWN": ("UNCONFIRMED", ""),
}

# Health Net family codes / product names that are HMO networks (used only when no explicit network word)
HMO_FAMILIES = {
    "SAL": ("Salud HMO y Mas (tailored HMO network); data: every other SAL-coded name in the index (163 rows) "
            "says HMO", SRC_TYPES),
    "SC": ("SmartCare (tailored HMO network)", SRC_TYPES),
    "EC": ("ExcelCare (tailored HMO network; also sold with EOA) - inferred from co-occurrence", SRC_TYPES),
    "CN": ("CanopyCare HMO (Bay Area) - broker source", SRC_CANOPY),
    "SALUD": ("Salud HMO y Mas", SRC_TYPES),
    "SMARTCARE": ("SmartCare HMO", SRC_TYPES),
    "WHOLECARE": ("WholeCare HMO", SRC_TYPES),
}

RX = {k: re.compile(p) for k, p in {
    "EOA": r"\bEOA\b",
    "POS": r"\bPOS\b",
    "EPO": r"\bEPO\b|\bEPO\$|-\s*EPO\b",
    "HMO": r"\bHMO\b",
    "PPO": r"\bPPO\b",
}.items()}
RX_FAMILY = re.compile(r"\b(" + "|".join(HMO_FAMILIES) + r")\b")


def clean_name(name: str) -> str:
    """Upper-case and split glued tokens such as 'CUSPPO' -> 'CUS PPO'."""
    s = (name or "").upper()
    return re.sub(r"CUS(?=PPO|HMO|EPO|POS|EOA)", "CUS ", s)


def name_segment(name: str) -> str:
    """Descriptive only: how the plan name starts (no meaning is assumed for L / T)."""
    for pfx in ("CA L&T", "CA L", "CA T"):
        if name.upper().startswith(pfx + " "):
            return pfx
    if re.match(r"^(BRONZE|SILVER|GOLD|PLATINUM)\b", name, re.I):
        return "metal-tier name"
    if name.upper().startswith("CALCHOICE"):
        return "CalChoice"
    return "other"


def classify(name: str) -> dict:
    s = clean_name(name)
    hits = [k for k in ("EOA", "POS", "EPO", "HMO", "PPO") if RX[k].search(s)]
    fam = RX_FAMILY.search(s)
    out = {"tokens_found": "+".join(hits), "family_code": fam.group(1) if fam else ""}

    def res(label, conf, rule, evidence, source=""):
        return {**out, "network_label": label, "confidence": conf, "rule": rule,
                "evidence": evidence, "source": source}

    if "EOA" in hits:
        return res("EOA_HYBRID", "high", "R1_EOA",
                   "name has EOA = Elect Open Access: Health Net calls it 'an HMO plan' (PCP via a physician group); "
                   "PPO tier limited to specialist exams/evaluations", f"{SRC_TYPES} ; {SRC_EOA}")
    if "POS" in hits:
        return res("POS", "high", "R2_POS", "name has POS (point-of-service)")
    if "EPO" in hits:
        conf, ev = "high", "name has EPO"
        src = ""
        if "PPO" in hits:
            conf = "medium"
            ev = ("name has both PPO and EPO. product_type = EPO because the name states EPO explicitly and the same "
                  "plan_id also files separate plain PPO/HMO/EOA/POS plans (index data, e.g. Sacramento City USD "
                  "946002491: CUS HMO, CUS PPO, CUS PPO - EPO). Health Net's EPO product runs on the separate "
                  "PureCare One EPO network (CA Dept. of Insurance filing), so the 'PPO' word is not counted")
            src = SRC_CDI_EPO
        return res("EPO", conf, "R3_EPO", ev, src)
    if "HMO" in hits:
        return res("HMO", "high", "R4_HMO", "name has HMO")
    if "PPO" in hits:
        return res("PPO", "high", "R5_PPO", "name has PPO" + (" (glued token fixed, e.g. CUSPPO)"
                                                              if "CUSPPO" in name.upper() else ""))
    if fam:
        desc, src = HMO_FAMILIES[fam.group(1)]
        return res("HMO", "medium", "R6_HMO_FAMILY",
                   f"no network word; family code {fam.group(1)} = {desc}", src)
    return res("UNKNOWN", "low", "R7_NONE", "no network word and no known family code")


def main():
    cfg = load_config()
    out_dir = cfg["paths"]["stage2"]
    out_dir.mkdir(parents=True, exist_ok=True)
    plans = pd.read_parquet(cfg["paths"]["stage1"] / "plans.parquet")
    ca = plans[plans.in_scope].copy()

    lab = pd.DataFrame([classify(n) for n in ca.plan_name], index=ca.index)
    out = pd.concat([ca[["plan_row_id", "plan_key", "plan_name", "plan_id", "plan_id_format", "plan_market_type",
                         "issuer", "structure_id"]], lab], axis=1)
    out = out.rename(columns={"network_label": "product_type"})
    out["provider_network"] = out.product_type.map(lambda t: NETWORK_OF[t][0])
    out["network_evidence"] = out.product_type.map(lambda t: NETWORK_OF[t][1])
    out["is_ppo"] = out.product_type.eq("PPO")                      # PPO plan (answers "which plans")
    out["uses_ppo_network"] = out.provider_network.eq("PPO_NETWORK")  # documented: PPO plans
    out["uses_ppo_network_incl_inferred"] = out.provider_network.isin(["PPO_NETWORK", "PPO_NETWORK_INFERRED"])
    out["name_segment"] = out.plan_name.map(name_segment)
    out["multi_network_words"] = out.tokens_found.str.contains(r"\+")
    out.to_parquet(out_dir / "plan_labels.parquet", index=False)
    save_csv(out, out_dir / "plan_labels.csv")

    print(f"in-scope plan rows: {len(out):,}  (distinct names {out.plan_name.nunique():,})\n")
    t = out.groupby(["product_type", "provider_network", "confidence", "rule"]).agg(rows=("plan_row_id", "size"),
                                                                  names=("plan_name", "nunique"))
    print(t.to_string())
    print("\nby product_type:", out.product_type.value_counts().to_dict())
    print("by provider_network:", out.provider_network.value_counts().to_dict())
    print("PPO plan rows:", int(out.is_ppo.sum()), " distinct PPO names:", out[out.is_ppo].plan_name.nunique(),
          " | on PPO network documented:", int(out.uses_ppo_network.sum()),
          " incl. inferred (EPO):", int(out.uses_ppo_network_incl_inferred.sum()))
    print("\nlabel x name_segment:\n", pd.crosstab(out.product_type, out.name_segment).to_string())
    print("\nnon-high-confidence or multi-word names:")
    for r in out[(out.confidence != "high") | out.multi_network_words].drop_duplicates("plan_name").itertuples():
        print(f"  [{r.product_type:10s} {r.confidence:6s} {r.rule:14s}] {r.plan_name}")
    print(f"\nwrote {out_dir / 'plan_labels.parquet'}")


if __name__ == "__main__":
    main()
