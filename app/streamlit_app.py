"""Bonus UI: executive and analyst views over the pipeline outputs.

Run (from project root, venv active):  streamlit run app/streamlit_app.py
Reads only files written by the pipeline (paths from config.yaml).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.common.normalize import ROOT, load_config  # noqa: E402
from src.stage3_scorecard import W_IN  # noqa: E402

# Reference palette (dataviz skill): categorical slots 1-3, ordinal blue ramp, text tokens
CAT = ["#2a78d6", "#eb6834", "#1baf7a"]
TIER_COLORS = {"VERIFIED": "#184f95", "PROBABLE": "#3987e5", "UNVERIFIED": "#86b6ef", "NONE": "#c3c2b7"}
INK = "#52514e"

st.set_page_config(page_title="Centene PPO Rates", layout="wide")
cfg = load_config()
P = cfg["paths"]


@st.cache_data
def load():
    d = {
        "summary": json.loads((ROOT / "data" / "summary.json").read_text(encoding="utf-8")),
        "scores": pd.read_parquet(P["stage3"] / "scorecard_in_network.parquet"),
        "scores_aa": pd.read_parquet(P["stage3"] / "scorecard_allowed.parquet"),
        "scores_ix": pd.read_parquet(P["stage3"] / "scorecard_index.parquet"),
        "labels": pd.read_parquet(P["stage2"] / "plan_labels.parquet"),
        "rf": pd.read_parquet(P["stage6"] / "provider_rate_file.parquet"),
        "addr": pd.read_parquet(P["stage5"] / "npi_tin_address.parquet"),
    }
    ce = P["stage7"] / "provider_cost_effectiveness.parquet"
    d["ce"] = pd.read_parquet(ce) if ce.exists() else pd.DataFrame()
    return d


D = load()
S = D["summary"]


def axis_cfg(chart):
    return chart.configure_axis(labelColor=INK, titleColor=INK, gridColor="#e6e5e0", domainColor="#c3c2b7") \
        .configure_view(strokeWidth=0)


view = st.sidebar.radio("View", ["Executive", "Analyst"])
st.sidebar.caption(f"Run date: {S['run_date']}")

if view == "Executive":
    st.title("Centene PPO network: California commercial group")
    c = st.columns(5)
    c[0].metric("PPO plans", S["ppo_plans"], help="of %d CA group plans" % S["ca_commercial_group_plans"])
    c[1].metric("PPO rate files", len(S["ppo_rate_files"]))
    c[2].metric("Rate rows (final file)", f"{S['final_rate_file']['rows']:,}")
    c[3].metric("NPIs with verified address", f"{S['address_linkage']['pct_npis_with_verified_address']}%")
    c[4].metric("TINs with verified address", f"{S['address_linkage']['pct_tins_with_verified_address']}%")

    st.subheader("From index files to the PPO network")
    funnel = pd.DataFrame({"step": [f"Plan rows in {S['index_files_parsed']} index files", "CA commercial group plans", "PPO plans"],
                           "plans": [S["plan_rows"], S["ca_commercial_group_plans"], S["ppo_plans"]]})
    ch = alt.Chart(funnel).mark_bar(color=CAT[0], cornerRadiusEnd=4, height=22).encode(
        x=alt.X("plans:Q", title="Plans"), y=alt.Y("step:N", sort=None, title=None),
        tooltip=["step", alt.Tooltip("plans:Q", format=",")])
    st.altair_chart(axis_cfg(ch + ch.mark_text(align="left", dx=4, color=INK).encode(text=alt.Text("plans:Q", format=","))),
                    use_container_width=True)
    lab = D["labels"].product_type.value_counts().rename_axis("product_type").reset_index(name="plans")
    st.caption("Plan labels: " + ", ".join(f"{r.product_type} {r.plans}" for r in lab.itertuples())
               + ". EOA (Elect Open Access) is defined by Health Net as an HMO plan.")

    st.subheader("File quality (C3 scorecard)")
    sc = D["scores"]
    metrics = list(W_IN)
    long = sc.melt(id_vars=["file_name"], value_vars=metrics, var_name="metric", value_name="score")
    long["file"] = long.file_name.str.extract(r"llc_([a-z\-]+)_in-network")[0]
    files = list(long.file.unique())[:3]
    ch = alt.Chart(long[long.file.isin(files)]).mark_bar(cornerRadiusEnd=4).encode(
        y=alt.Y("metric:N", title=None), x=alt.X("score:Q", scale=alt.Scale(domain=[0, 100]), title="Score (0-100)"),
        yOffset="file:N", color=alt.Color("file:N", scale=alt.Scale(domain=files, range=CAT[:len(files)]), title="File"),
        tooltip=["file", "metric", alt.Tooltip("score:Q", format=".1f")])
    st.altair_chart(axis_cfg(ch), use_container_width=True)
    st.dataframe(sc[["role", "file_name", "overall_score"] + metrics], hide_index=True, use_container_width=True)
    st.caption("Allowed-amounts files and index files are graded too (docs/C3_file_quality_metrics.md):")
    st.dataframe(D["scores_aa"][["role", "file_name", "overall_score"]], hide_index=True, use_container_width=True)
    st.dataframe(D["scores_ix"][["index_name", "overall_score", "Price Reachability", "Plan ID Integrity"]],
                 hide_index=True, use_container_width=True)

    st.subheader("Address linkage (C5)")
    best = D["addr"][D["addr"].is_best].tier.value_counts().reindex(list(TIER_COLORS)).fillna(0).astype(int)
    tdf = best.rename_axis("tier").reset_index(name="pairs")
    ch = alt.Chart(tdf).mark_bar(cornerRadiusEnd=4, height=22).encode(
        x=alt.X("pairs:Q", title="(NPI, TIN) pairs - best address"), y=alt.Y("tier:N", sort=list(TIER_COLORS), title=None),
        color=alt.Color("tier:N", scale=alt.Scale(domain=list(TIER_COLORS), range=list(TIER_COLORS.values())), legend=None),
        tooltip=["tier", alt.Tooltip("pairs:Q", format=",")])
    st.altair_chart(axis_cfg(ch + ch.mark_text(align="left", dx=4, color=INK).encode(text="pairs:Q")),
                    use_container_width=True)

    if len(D["ce"]):
        st.subheader("Cost effectiveness (C7)")
        k = st.columns(2)
        k[0].write(D["ce"].label.value_counts().rename_axis("price label").reset_index(name="providers"))
        k[1].write(D["ce"].value_label.value_counts().rename_axis("value label (price x CMS quality)")
                   .reset_index(name="providers"))

else:
    st.title("Analyst view: provider rates")
    rf = D["rf"]
    f1, f2, f3, f4, f5 = st.columns(5)
    code = f1.selectbox("Billing code", ["(all)"] + sorted(rf["Billing Code"].unique()))
    specs = f2.multiselect("Specialty", sorted(rf.Specialty.dropna().unique()))
    tiers = f3.multiselect("Address tier", list(TIER_COLORS), default=["VERIFIED", "PROBABLE"])
    comp = f4.selectbox("Fee type", ["(all)", "professional", "facility"],
                        help="professional = doctor's fee; facility = hospital/ASC fee for the same code. Never mixed in C7")
    strict = f5.checkbox("Strict view: HIGH trust only", value=False,
                         help="trust_band HIGH (rate check, contracts, contract formula, PPO evidence, address)")
    q = rf.copy()
    if code != "(all)":
        q = q[q["Billing Code"] == code]
    if specs:
        q = q[q.Specialty.isin(specs)]
    if tiers:
        q = q[q.address_tier.isin(tiers)]
    if comp != "(all)":
        q = q[q.rate_component.eq(comp)]
    if strict:
        q = q[q.trust_band.eq("HIGH")]
    m = st.columns(4)
    m[0].metric("Rows", f"{len(q):,}")
    m[1].metric("NPIs", f"{q.NPI.nunique():,}")
    m[2].metric("TINs", f"{q.TIN.nunique():,}")
    # a doctor's fee and a hospital fee are different prices: one median per fee type, never blended
    med = q.groupby("rate_component").Rate.median()
    m[3].metric("Median rate" + (f" ({med.index[0]})" if len(med) == 1 else ""),
                (f"${med.iloc[0]:,.2f}" if len(med) == 1 else
                 " / ".join(f"{k} ${v:,.0f}" for k, v in med.items())) if len(q) else "-")
    if code != "(all)" and len(q):
        st.caption(f"{code}: {q.item_name.iloc[0]}")
        ch = alt.Chart(q.drop_duplicates(["NPI", "TIN", "rate_component"])).mark_bar(cornerRadiusEnd=4).encode(
            x=alt.X("Rate:Q", bin=alt.Bin(maxbins=40), title="Headline rate (USD)"),
            y=alt.Y("count():Q", title="Providers"),
            color=alt.Color("rate_component:N", scale=alt.Scale(domain=["professional", "facility"], range=CAT[:2]),
                            title="Fee type"),
            tooltip=["rate_component", alt.Tooltip("count():Q", title="Providers")])
        st.altair_chart(axis_cfg(ch), use_container_width=True)
    st.caption("PPO network only: providers with HMO evidence are in the Excel sheet 'Excluded - HMO signals'.")
    st.dataframe(q[["NPI", "TIN", "provider_name", "Address", "Billing Code", "item_name", "Specialty", "Rate",
                    "trust_score", "pct_of_medicare", "rate_check", "address_tier", "address_expected_accuracy",
                    "rate_component", "place_of_service", "modifier", "ppo_network_evidence"]],
                 hide_index=True, use_container_width=True)
    if len(D["ce"]):
        st.subheader("Cost effectiveness of the providers shown")
        ce = D["ce"].merge(q[["NPI", "TIN"]].drop_duplicates(), on=["NPI", "TIN"])
        st.dataframe(ce.sort_values("weighted_cost_index"), hide_index=True, use_container_width=True)
