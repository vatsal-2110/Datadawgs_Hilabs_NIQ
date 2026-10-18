"""C4 add-on: reverse-engineer each provider's contract formula, and estimate its missing HiLabs rates.

1 CONTRACT FORMULA. Health Net's professional schedule is Medicare-pegged (src.stage4_medicare_benchmark): contracts
  read "pay X% of Medicare". The file only carries the resulting dollars, so X is recovered per provider (NPI, TIN):
  every professional rate with a Medicare price gives pct = rate / Medicare (provider's own CA locality) x 100; the
  multiplier is the value that the most of the provider's codes agree on (within +-1%), refined as their median.
    formula_class  EXACT   >= 2 codes, >= 80% of them on the multiplier
                   LIKELY  >= 2 codes, >= 50% on it
                   SINGLE  1 code (multiplier = that code's pct; cannot be confirmed)
                   NONE    >= 2 codes, no multiplier shared by half of them (no single formula)
  The same fit is run per TIN (all NPIs of a group usually share one contract); a provider whose own formula is
  SINGLE / NONE inherits an EXACT TIN formula (formula_source = TIN).

2 ESTIMATED RATES (never mixed with real rates). For a provider with a trusted formula, each HiLabs code of its
  specialty that the file does not price for it: estimated_rate = multiplier x Medicare (non-facility, own locality).
  Shipped only for formula groups that pass a hide-and-check test (leave-one-out): each known rate is hidden, the
  formula is refit on the provider's (or TIN's) other codes and the hidden rate predicted. A group ships only if
  >= MIN_ACCURACY of its predictions land within 5% of the true rate; the baseline (statewide median rate of the
  code) is reported next to it.

Inputs : data/stage4/rates.parquet (with Medicare columns), data/stage5/npi_specialty.parquet, CMS PFS zip, HiLabs xlsx
Outputs: data/stage4/provider_contract_terms.parquet (+ .csv), data/stage4/estimated_rates.parquet (+ .csv),
         columns contract_multiplier_pct / fits_contract_formula added to rates.parquet, data/stage4/c4_contract_terms.txt
Usage  : python -m src.stage4_contract_terms
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.common.normalize import load_config, save_csv
from src.stage4_medicare_benchmark import load_pfs

TOL, MIN_ACCURACY = 0.01, 0.70
TERM_COLS = ["contract_multiplier_pct", "fits_contract_formula"]


def fit(pcts: np.ndarray) -> tuple[float, int]:
    """Multiplier that most values agree on (+-TOL relative), refined as the median of those values; (k, n agreeing)."""
    if len(pcts) == 0:
        return np.nan, 0
    agree = (np.abs(pcts[None, :] / pcts[:, None] - 1) <= TOL).sum(axis=1)
    best = agree.max()
    # tie-break: the candidate nearest a whole-number percentage (contracts are written that way)
    cand = np.where(agree == best)[0]
    c = cand[np.argmin(np.abs(pcts[cand] - np.round(pcts[cand])))]
    on = np.abs(pcts / pcts[c] - 1) <= TOL
    return float(np.median(pcts[on])), int(best)


def classify(n: int, n_fit: int) -> str:
    if n == 1:
        return "SINGLE"
    share = n_fit / n
    return "EXACT" if share >= 0.8 else ("LIKELY" if share >= 0.5 else "NONE")


def main():
    cfg = load_config()
    P = cfg["paths"]
    r = pd.read_parquet(P["stage4"] / "rates.parquet")
    r = r.drop(columns=[c for c in TERM_COLS if c in r.columns])
    r["_n"] = r.npi.fillna("")
    use = r.component.eq("professional") & r.pct_of_medicare.notna() & r.rate_check.eq("OK")
    # one pct per provider-code (several places of service of the same code are one contract term)
    u = r[use].groupby(["_n", "tin", "billing_code"]).pct_of_medicare.median().reset_index()

    # ---- 1 contract formula per provider and per TIN
    rows = []
    for (n, t), d in u.groupby(["_n", "tin"]):
        k, nf = fit(d.pct_of_medicare.values)
        rows.append((n, t, len(d), round(k, 1), nf, classify(len(d), nf)))
    ct = pd.DataFrame(rows, columns=["_n", "tin", "n_codes", "multiplier_pct", "n_codes_on_multiplier", "formula_class"])
    trows = []
    for t, d in u.groupby("tin"):
        k, nf = fit(d.pct_of_medicare.values)
        trows.append((t, len(d), d._n.nunique(), round(k, 1), nf, classify(len(d), nf)))
    tt = pd.DataFrame(trows, columns=["tin", "tin_n_rates", "tin_n_npis", "tin_multiplier_pct", "tin_n_on_multiplier",
                                      "tin_formula_class"])
    ct = ct.merge(tt, on="tin", how="left")
    inherit = ct.formula_class.isin(["SINGLE", "NONE"]) & ct.tin_formula_class.eq("EXACT") & (ct.tin_n_npis > 1)
    ct["formula_source"] = np.where(inherit, "TIN", "own")
    ct["used_multiplier_pct"] = np.where(inherit, ct.tin_multiplier_pct, ct.multiplier_pct)
    ct["used_formula_class"] = np.where(inherit, "EXACT", ct.formula_class)
    ct["whole_number_pct"] = (ct.used_multiplier_pct - ct.used_multiplier_pct.round()).abs() <= 0.05

    # ---- hide-and-check (leave-one-out) on every known professional rate
    tin_rows = {t: d for t, d in u.groupby("tin")}
    loo = []
    for (n, t), d in u.groupby(["_n", "tin"]):
        own_cls = ct.loc[(ct._n == n) & (ct.tin == t), "formula_class"].iloc[0]
        for i in range(len(d)):
            true = d.pct_of_medicare.values[i]
            rest = np.delete(d.pct_of_medicare.values, i)
            if len(rest) >= 1:
                k, nf = fit(rest)
                cls = classify(len(rest), nf)
                loo.append(("own", cls, own_cls, true, k))
            # TIN formula fitted without this provider
            other = tin_rows[t][tin_rows[t]._n != n].pct_of_medicare.values
            if len(other) >= 2:
                k, nf = fit(other)
                if classify(len(other), nf) == "EXACT":
                    loo.append(("TIN", "EXACT", own_cls, true, k))
    L = pd.DataFrame(loo, columns=["source", "cls", "own_class", "true_pct", "pred_pct"])
    L["err"] = (L.pred_pct / L.true_pct - 1).abs()
    acc = L.groupby(["source", "cls"]).agg(tests=("err", "size"), within_1pct=("err", lambda e: (e <= .01).mean()),
                                           within_5pct=("err", lambda e: (e <= .05).mean()),
                                           median_abs_err=("err", "median")).reset_index()
    # baseline: statewide median rate of the code (what a user would guess without a formula)
    std = r[use]
    base = std.groupby("billing_code").rate.transform("median")
    b_err = (base / std.rate - 1).abs()
    acc["ships"] = acc.within_5pct >= MIN_ACCURACY
    ship = {(s, c) for s, c, ok in zip(acc.source, acc.cls, acc.ships) if ok}

    # ---- 2 estimated rates for HiLabs codes of the provider's specialty not priced for it
    pfs, gp, _ = load_pfs(P["cms_pfs"] / "rvu26d.zip")
    pf = pfs[pfs["mod"].eq("")].set_index("code")
    loc_name = dict(zip(gp.locality, gp["loc"]))
    G, gmed = gp.set_index("loc"), gp[["g_work", "g_pe", "g_mp"]].median()
    ploc = r[r.component.eq("professional")].groupby(["_n", "tin"]).medicare_locality.agg(
        lambda s: s.mode().iloc[0]).rename("medicare_locality").reset_index()
    spec = pd.read_parquet(P["stage5"] / "npi_specialty.parquet")[["npi", "spec_cd", "specialty"]]
    sp = pd.read_excel(P["hilabs_xlsx"], sheet_name="Spec-Proc Weights", dtype=str)
    spec_codes = sp.groupby("SPEC_CD").PROC_CD.agg(set).to_dict()
    priced = r.groupby(["_n", "tin"]).billing_code.agg(set).to_dict()
    ct = ct.merge(ploc, on=["_n", "tin"], how="left").merge(spec.rename(columns={"npi": "_n"}), on="_n", how="left")
    ct["estimates_ship"] = [((s, c) in ship) for s, c in zip(ct.formula_source, ct.used_formula_class)]
    est = []
    for e in ct[ct.estimates_ship & ct.spec_cd.notna()].rename(columns={"_n": "nk"}).itertuples():
        lc = loc_name.get(e.medicare_locality)
        gw, gpe, gmp = ((G.loc[lc, c] if lc in G.index else gmed[c]) for c in ("g_work", "g_pe", "g_mp"))
        for code in sorted(spec_codes.get(e.spec_cd, set()) - priced.get((e.nk, e.tin), set())):
            if code not in pf.index:
                continue
            x = pf.loc[code]
            mc = round((x.work * gw + x.pe_nf * gpe + x.mp * gmp) * x.cf, 2)
            if not mc > 0:
                continue
            est.append((e.nk or None, e.tin, code, e.spec_cd, e.specialty, e.medicare_locality, round(mc, 2),
                        e.used_multiplier_pct, round(mc * e.used_multiplier_pct / 100, 2), e.used_formula_class,
                        e.formula_source))
    E = pd.DataFrame(est, columns=["npi", "tin", "billing_code", "spec_cd", "specialty", "medicare_locality",
                                   "medicare_rate_nonfacility", "multiplier_pct", "estimated_rate", "formula_class",
                                   "formula_source"])
    a = acc.set_index(["source", "cls"])
    E["validated_within_5pct"] = [round(a.loc[(s, c), "within_5pct"], 3) for s, c in zip(E.formula_source, E.formula_class)]
    E["confidence"] = np.where(E.formula_source.eq("own") & E.formula_class.eq("EXACT"), "HIGH", "MEDIUM")
    E["is_estimate"] = True
    E.to_parquet(P["stage4"] / "estimated_rates.parquet", index=False)
    save_csv(E, P["stage4"] / "estimated_rates.csv")

    # ---- write terms + per-row fit back to the rates
    out = ct.assign(npi=ct._n.replace("", None)).drop(columns="_n")
    out.to_parquet(P["stage4"] / "provider_contract_terms.parquet", index=False)
    save_csv(out, P["stage4"] / "provider_contract_terms.csv")
    km = ct.set_index(["_n", "tin"]).used_multiplier_pct
    ok_cls = ct.set_index(["_n", "tin"]).used_formula_class.isin(["EXACT", "LIKELY"])
    key = pd.MultiIndex.from_arrays([r._n, r.tin])
    r["contract_multiplier_pct"] = np.where(ok_cls.reindex(key, fill_value=False).values.astype(bool), km.reindex(key).values, np.nan)
    fits = (r.pct_of_medicare / r.contract_multiplier_pct - 1).abs() <= TOL
    r["fits_contract_formula"] = pd.Series(np.where(r.contract_multiplier_pct.notna() & r.pct_of_medicare.notna()
                                                    & r.component.eq("professional"), fits, None), index=r.index,
                                           dtype="object")
    r = r.drop(columns="_n")
    r.to_parquet(P["stage4"] / "rates.parquet", index=False)
    save_csv(r, P["stage4"] / "rates.csv")

    # ---- report
    vc = ct.formula_class.value_counts()
    top = ct[ct.used_formula_class.eq("EXACT")].used_multiplier_pct.round().value_counts().head(8)
    R = ["C4 contract terms: each provider's formula, recovered from its rates", "=" * 78,
         f"Providers with >= 1 benchmarked professional rate: {len(ct):,}",
         "Own formula: " + ", ".join(f"{k} {vc.get(k, 0):,}" for k in ("EXACT", "LIKELY", "SINGLE", "NONE")),
         f"Inherited from an EXACT TIN formula (own SINGLE/NONE): {int(inherit.sum()):,}",
         f"Formula at a whole-number % of Medicare (EXACT): {int(ct[ct.used_formula_class.eq('EXACT')].whole_number_pct.sum()):,} "
         f"of {int(ct.used_formula_class.eq('EXACT').sum()):,}",
         "Most common EXACT contract levels (% of Medicare): " + ", ".join(f"{int(k)}% ({v})" for k, v in top.items()),
         "",
         "Hide-and-check test (leave-one-out: hide a known rate, refit on the other codes, predict it):",
         acc.round(3).to_string(index=False),
         f"   baseline without a formula (statewide median rate of the code): within 5% for "
         f"{(b_err <= .05).mean():.1%} of rates, median error {b_err.median():.1%}",
         f"   groups shipped (within 5% >= {MIN_ACCURACY:.0%}): {sorted(ship)}",
         "",
         f"Estimated rates (separate file, is_estimate = True): {len(E):,} rows | providers {E.npi.nunique():,} | "
         f"codes {E.billing_code.nunique()} | confidence {E.confidence.value_counts().to_dict()}",
         f"Real rows fitting their provider's formula (+-1%): {int((r.fits_contract_formula == True).sum()):,} of "  # noqa: E712
         f"{int(r.fits_contract_formula.notna().sum()):,} professional rows with a formula"]
    (P["stage4"] / "c4_contract_terms.txt").write_text("\n".join(R) + "\n", encoding="utf-8")
    print("\n".join(R))


if __name__ == "__main__":
    main()
