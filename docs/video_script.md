# Video script (≤ 5 minutes)

**Setup:** record your screen with the Streamlit UI (`streamlit run app/streamlit_app.py`) and the docs open. About 130 words per minute.
Every number comes from `docs/summary.md`; if you re-run the pipeline, read the new numbers from there.

---

### 0:00–0:30 · The problem and the answer (show: Executive view, top tiles)
"Centene publishes its negotiated rates in huge machine-readable files. Our job was to find its **PPO network** for **California
commercial group**, grade those files, and tie every provider to a **real practicing address**.
Result: **453 PPO plans**, served by **one** rate file. From it we built a rate file of **11,724 rows** at NPI × TIN × address ×
billing code, and **44% of NPIs and 41% of TINs** are linked to a **verified** address."

### 0:30–1:30 · Finding the PPO network (show: funnel chart, then `docs/logic_ppo_network.md` §3)
- "We parsed all **11** Centene index files: 3,275 plan rows, **66 unique rate files** after removing duplicate links."
- "Scope came from the data, not the brand. Ambetter is individual market. Fidelis IDs say New York, QualChoice says Arkansas.
  And a trap: Health Net's index also contains **Oregon**, 381 plans we would have pulled in by brand alone."
- "The key finding: **all 1,312 California plans, PPO and HMO alike, point to one single file.** So the network can't be found by
  picking files; it has to be found plan by plan."
- "Plan names carry PPO, HMO, EOA, POS. We researched what we didn't know. Health Net itself says **'EOA is an HMO plan'**, so EOA is
  not PPO. Names with both PPO and EPO get two labels: EPO for the benefit rules, PPO network for the doctors."
- "**453 PPO plans.** Ruled out: 723 HMO, 127 EOA, 6 POS, 3 EPO."

### 1:30–2:15 · Can providers be split inside the file? (show: logic §4)
- "We tested five ways. The network field is empty in every provider group. Each provider has **one** price per service:
  under 0.5% have two. So PPO and HMO contracts are not mixed in a way we could separate."
- "Honesty point: our first test said 43%. We found that the extra prices were sub-codes, not networks, and fixed it."
- "Decision: the whole file is the PPO network's rate source. HMO-style providers are **flagged, not dropped**, about 1% of rows,
  and we report results with and without them."

### 2:15–3:00 · Grading the files (show: File quality chart + table)
- "Seven metrics, each a plain percentage with a written formula: code coverage weighted by HiLabs' own weights, provider detail,
  rate validity with a robust outlier test, usable rate types, duplicates and conflicts, freshness against the monthly
  update rule, and ID validity using the NPI check digit."
- "To make a score meaningful we grade **peers** from the same payer. Our file scores **79**: current and clean, but only
  **20% of its NPIs are individual clinicians**, against 98% in Centene's own marketplace file. That's a real limitation of the data."

### 3:00–4:15 · TIN → NPI → address (show: Address linkage chart, then `docs/logic_address_mapping.md`)
- "70% of the NPPES records here weren't updated in two years, so we never trust one source."
- "Every address comes from independent sources: NPPES, Medicare's Doctors & Clinicians file, and CMS facility registries
  for hospitals, surgery centres, equipment suppliers and home health. The US Census geocoder standardizes them and confirms they exist."
- "The rate file's own structure links TIN to address: if another doctor billing under the **same TIN** practises at that address,
  that confirms it."
- "Verified means **two independent pieces of evidence agree**. We caught and removed circular evidence on the way: one medical group's 194
  sites were being copied to every one of its NPIs."
- "One TIN can have many addresses: 378 TINs and 317 NPIs have more than one verified or probable site, and every one stays in the file."

### 4:15–4:50 · The rate file, UI and bonus (show: Analyst view, filter 99213; then Excel)
- "One rate per row: the standard, no-modifier office rate. Every variant, like telehealth, is in a second sheet."
- "The analyst view filters by code, specialty and address tier. Cost effectiveness compares providers on the same code, in the same
  specialty and county, weighted by what matters for that specialty."

### 4:50–5:00 · Close
"Everything re-runs with one command from the README, streams large files, runs in parallel, and passes 38 independent checks,
including a full recomputation of every extracted rate."
