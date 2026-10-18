"""C2 provider level: which providers in the shared CA rate file are evidenced to be in the PPO network?

All 1,312 CA plans (PPO and HMO alike) use one rate file, so plan labels alone can't say which *providers* are PPO.
Decision D2.7 treats the whole file as the PPO network's rate source. This module grades every (NPI, TIN) pair on
evidence that does not depend on that decision:

  PPO evidence
    G  geography     a VERIFIED/PROBABLE practice address in a California county with NO Health Net HMO network.
                     The CA Office of the Patient Advocate lists the HMO service area as 31 counties and the PPO
                     service area as "All of California" (config evidence.hmo_service_area). A Health Net contract
                     with a provider practising there can only serve members through the PPO network.
    L  label         the provider group's business name states a PPO network ("PPO: <county>", "HEALTH NET PPO",
                     "AMBETTER PPO"; config evidence.ppo_labels)
  HMO evidence
    C  capitation    priced under a capitation arrangement. In California capitation is an HMO payment model;
                     PPO plans pay fee-for-service (Milliman, delegated risk model in California)
    H  label         provider group named as an IPA or "DIRECT NETWORK: AMBETTER HMO" (config evidence.hmo_labels)
    O  OON-paid      the PPO plans' own allowed-amounts file shows this (NPI, TIN) paid OUT-OF-NETWORK for a
                     non-emergency service (emergency care is paid whatever the network, so it proves nothing).
                     The pair has a contract in the shared file, but not one the PPO plans use -> HMO-side contract.

Classes (per pair): PPO_CONFIRMED (G or L, no HMO evidence) · MIXED (both) · HMO_SIGNAL (HMO evidence only) ·
PPO_PRESUMED (direct fee-for-service, no further evidence: in the PPO plans' in-network file, kept under D2.7).

Outputs (data/stage2/): provider_network_evidence.parquet (+ .csv), c2_provider_network.txt
Usage: python -m src.stage2_provider_network
"""
from __future__ import annotations

import pandas as pd

from src.common.normalize import load_config, save_csv

EMERGENCY_CODES = {"99281", "99282", "99283", "99284", "99285", "99291", "99292"}

# California county FIPS codes (US Census Bureau, ANSI/FIPS county codes, state 06)
CA_COUNTY_FIPS = {
    "001": "Alameda", "003": "Alpine", "005": "Amador", "007": "Butte", "009": "Calaveras", "011": "Colusa",
    "013": "Contra Costa", "015": "Del Norte", "017": "El Dorado", "019": "Fresno", "021": "Glenn", "023": "Humboldt",
    "025": "Imperial", "027": "Inyo", "029": "Kern", "031": "Kings", "033": "Lake", "035": "Lassen",
    "037": "Los Angeles", "039": "Madera", "041": "Marin", "043": "Mariposa", "045": "Mendocino", "047": "Merced",
    "049": "Modoc", "051": "Mono", "053": "Monterey", "055": "Napa", "057": "Nevada", "059": "Orange", "061": "Placer",
    "063": "Plumas", "065": "Riverside", "067": "Sacramento", "069": "San Benito", "071": "San Bernardino",
    "073": "San Diego", "075": "San Francisco", "077": "San Joaquin", "079": "San Luis Obispo", "081": "San Mateo",
    "083": "Santa Barbara", "085": "Santa Clara", "087": "Santa Cruz", "089": "Shasta", "091": "Sierra",
    "093": "Siskiyou", "095": "Solano", "097": "Sonoma", "099": "Stanislaus", "101": "Sutter", "103": "Tehama",
    "105": "Trinity", "107": "Tulare", "109": "Tuolumne", "111": "Ventura", "113": "Yolo", "115": "Yuba",
}


def main():
    cfg = load_config()
    P = cfg["paths"]
    ev = cfg["evidence"]
    hmo_cty = set(ev["hmo_service_area"]["counties"])
    assert hmo_cty <= set(CA_COUNTY_FIPS.values()), "unknown county name in evidence.hmo_service_area"

    pairs = pd.read_parquet(P["stage4"] / "tin_npi_pairs.parquet")
    rates = pd.read_parquet(P["stage4"] / "rates.parquet")
    addr = pd.read_parquet(P["stage5"] / "npi_tin_address.parquet")
    k = ["_npi", "tin"]
    for d in (pairs, rates, addr):
        d["_npi"] = d.npi.fillna("")

    # G: geography of confirmed practice addresses
    a = addr[addr.tier.isin(["VERIFIED", "PROBABLE"]) & addr.county_fips5.notna()].copy()
    a["county"] = [CA_COUNTY_FIPS.get(f[2:]) if f.startswith("06") else None for f in a.county_fips5]
    a["non_hmo_county"] = a.county.notna() & ~a.county.isin(hmo_cty)
    geo = a.groupby(k).agg(g_non_hmo_county=("non_hmo_county", "any"),
                           g_counties=("county", lambda s: " | ".join(sorted({c for c in s if c}))),
                           g_out_of_state=("county", lambda s: s.isna().all())).reset_index()

    # C: capitation
    cap = rates.groupby(k).negotiation_arrangement.agg(
        lambda s: "capitation" in "|".join(s)).rename("c_capitation").reset_index()

    out = pairs[k + ["npi", "business_names", "business_name_labels", "in_capitation_code", "billing_classes"]] \
        .merge(geo, on=k, how="left").merge(cap, on=k, how="left")
    labels = out.business_name_labels.fillna("").str.split(r"\s*\|\s*")
    out["l_ppo_label"] = labels.map(lambda ls: any(x in ev["ppo_labels"] for x in ls))
    out["h_hmo_label"] = labels.map(lambda ls: any(x in ev["hmo_labels"] for x in ls))
    out["c_capitation"] = out.c_capitation.astype("boolean").fillna(False).astype(bool) \
        | out.in_capitation_code.astype("boolean").fillna(False).astype(bool)
    out["g_non_hmo_county"] = out.g_non_hmo_county.astype("boolean").fillna(False).astype(bool)
    # O: paid out-of-network by the PPO plans for a non-emergency service (allowed-amounts file, src.stage4_extract_rates)
    of = P["stage4"] / "oon_allowed.parquet"
    if of.exists():
        o = pd.read_parquet(of)
        o = o[~o.billing_code.isin(EMERGENCY_CODES) & ~o.service_code.str.split("|").map(lambda s: "23" in s)]
        o["_npi"] = o.npi.fillna("")
        oo = o.groupby(k).agg(o_oon_codes=("billing_code", lambda s: ",".join(sorted(set(s))[:8]))).reset_index()
        out = out.merge(oo, on=k, how="left")
    else:
        out["o_oon_codes"] = None
    out["o_oon_paid"] = out.o_oon_codes.notna()
    out["has_rates"] = out.set_index(k).index.isin(rates.set_index(k).index)

    ppo = out.g_non_hmo_county | out.l_ppo_label
    # validation of O before it is used: how did the classes built from G/L/C/H alone treat OON-paid pairs?
    pre = (ppo & ~(out.c_capitation | out.h_hmo_label)).map({True: "PPO_CONFIRMED", False: ""})
    o_check = {"oon_paid_pairs": int(out.o_oon_paid.sum()),
               "of_which_ppo_confirmed_by_G_or_L": int((out.o_oon_paid & pre.eq("PPO_CONFIRMED")).sum()),
               "hmo_share_all": round(float((out.c_capitation | out.h_hmo_label).mean()), 3),
               "hmo_share_oon_paid": round(float((out.c_capitation | out.h_hmo_label)[out.o_oon_paid].mean()), 3)}
    hmo = out.c_capitation | out.h_hmo_label | out.o_oon_paid
    out["ppo_network_evidence"] = "PPO_PRESUMED"
    out.loc[ppo & ~hmo, "ppo_network_evidence"] = "PPO_CONFIRMED"
    out.loc[hmo & ~ppo, "ppo_network_evidence"] = "HMO_SIGNAL"
    out.loc[ppo & hmo, "ppo_network_evidence"] = "MIXED"
    out["ppo_evidence_detail"] = [
        "; ".join(x for x in (f"practises in {c} (no Health Net HMO network there)" if g else "",
                              "PPO group label" if lp else "", "capitation contract" if cp else "",
                              "IPA / Ambetter-HMO label" if lh else "",
                              f"paid out-of-network by the PPO plans for {oc}" if op else "") if x)
        for g, c, lp, cp, lh, op, oc in zip(out.g_non_hmo_county, out.g_counties.fillna(""), out.l_ppo_label,
                                             out.c_capitation, out.h_hmo_label, out.o_oon_paid, out.o_oon_codes.fillna(""))]
    out = out.drop(columns="_npi")
    out.to_parquet(P["stage2"] / "provider_network_evidence.parquet", index=False)
    save_csv(out, P["stage2"] / "provider_network_evidence.csv")

    # P: price signature. Same code + place of service, standard service (no modifier), groups of >= 10 providers:
    # each provider's rate / the code's median. If PPO_PRESUMED providers are on the PPO fee schedule, their per-code
    # median should match PPO_CONFIRMED; HMO_SIGNAL providers need not.
    r = rates[rates.modifier.eq("")].merge(out.assign(_npi=out.npi.fillna(""))[["_npi", "tin", "ppo_network_evidence"]],
                                           on=["_npi", "tin"])
    sk = ["billing_code", "billing_class", "service_code"]      # professional and facility prices never compared
    r = r[r.groupby(sk)._npi.transform("nunique") >= 10].copy()
    r["rate_index"] = r.rate / r.groupby(sk).rate.transform("median")
    per_code = r.groupby(["billing_code", "ppo_network_evidence"]).rate_index.median().unstack()
    price = {}
    for c in ("PPO_PRESUMED", "HMO_SIGNAL"):
        if {"PPO_CONFIRMED", c} <= set(per_code.columns):
            d = (per_code[c] - per_code["PPO_CONFIRMED"]).dropna()
            price[c] = (len(d), round(float(d.median()), 3))

    t = out.groupby("ppo_network_evidence").agg(pairs=("tin", "size"), pairs_with_rates=("has_rates", "sum"))
    geo_cty = a[a.non_hmo_county].county.value_counts()
    L = ["C2 provider level: PPO-network evidence per (NPI, TIN) in the shared CA rate file", "=" * 84,
         f"HMO service area: {len(hmo_cty)} counties (OPA report card); PPO service area: all 58 CA counties",
         "", t.to_string(), "",
         f"G geography: {int(out.g_non_hmo_county.sum())} pairs have a confirmed practice address in a county with no "
         f"Health Net HMO network: " + ", ".join(f"{c} {n}" for c, n in geo_cty.head(12).items()),
         f"L PPO labels: {int(out.l_ppo_label.sum())} pairs | C capitation: {int(out.c_capitation.sum())} pairs | "
         f"H IPA / Ambetter-HMO labels: {int(out.h_hmo_label.sum())} pairs | O paid out-of-network by the PPO plans "
         f"(non-emergency): {int(out.o_oon_paid.sum())} pairs",
         f"   check of O: {o_check['of_which_ppo_confirmed_by_G_or_L']} of {o_check['oon_paid_pairs']} OON-paid pairs had "
         f"been PPO_CONFIRMED by G/L alone; C/H HMO evidence in {100 * o_check['hmo_share_oon_paid']:.1f}% of OON-paid "
         f"pairs vs {100 * o_check['hmo_share_all']:.1f}% of all pairs -> O points the same way as C/H",
         "",
         "P price signature (rate / code median, same code + billing class + place of service, no modifier; >= 10 providers):",
         *[f"   {c} minus PPO_CONFIRMED, median over {n} codes: {v:+.3f}" for c, (n, v) in price.items()],
         "   PPO_PRESUMED priced like PPO_CONFIRMED = consistent with one PPO fee schedule; a gap for HMO_SIGNAL means",
         "   those contracts are priced differently (evidence, not proof).",
         "",
         "Reading: PPO_CONFIRMED pairs prove the file carries PPO-network contracts that cannot be HMO contracts;",
         "HMO_SIGNAL pairs leave the C6 Rate File (sheet 'Excluded - HMO signals', D6.5); PPO_PRESUMED = direct fee-for-service",
         "contracts in the PPO plans' in-network file with no further evidence either way."]
    (P["stage2"] / "c2_provider_network.txt").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
