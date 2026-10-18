# Video script (≤ 5 minutes)

**Setup:** record your screen with the Streamlit UI (`streamlit run app/streamlit_app.py`) and the docs open. About 130 words per minute.
Every number comes from `docs/summary.md`; if you re-run the pipeline, read the new numbers from there.

---

### 0:00–0:30 · The problem and the answer (show: Executive view, top tiles)
"Centene publishes its negotiated rates in huge machine-readable files. Our job was to find its **PPO network** for **California
commercial group**, grade those files, and tie every provider to a **real practicing address**.
Result: **453 plans use the PPO network**, all served by **one** rate file, and we proved it from Health Net's own plan documents,
not from plan names. From that file we built **10,287 rate rows** (professional and facility fees, 203 billing codes) at NPI × TIN × address × billing code; **45% of NPIs** have a
**verified** address."

### 0:30–1:00 · Which plans are California? Proven, not assumed (show: C1 Excel, Scope Evidence sheet)
- "We parsed all **11** Centene index files: 3,275 plan rows, **66 unique rate files** after removing duplicate links."
- "We never trusted the word 'CA'. For each group of plans we checked **where its doctors practise** (NPPES: 96.6% California),
  **where its employers are** (US Department of Labor filings: 76% California) and **who the licensed insurer is** (CMS: Health Net
  of California)."
- "Trap avoided: 381 Health Net plans have no state in their name. The same tests say **Oregon**: 97% of their doctors and 99% of
  their employers. Brand-level scoping would have pulled them in."

### 1:00–1:50 · Which plans use the PPO network? Without reading the name (show: C2 Excel, Design Catalog + Plan Network Use)
- "All 1,312 California plans, PPO and HMO alike, point to **one** rate file, and the file has **no network field**. So a plan
  name saying 'PPO' is only the payer's label. We needed proof."
- "The numbers in a plan name are its **benefit design**. We downloaded **all 450** of Health Net's 2026 plan documents and read
  what each says about itself: **every PPO document has out-of-network benefits and a Certificate of Insurance; no HMO or EOA
  document does.** And **no design is sold both ways**."
- "So we classified **90% of plans from design and documents alone**: 393 PPO plans proven, and the plan name never disagreed."
- "Hidden insight: EOA uses Health Net's **exact HMO benefit grids**, plus a PPO tier for office visits. So EOA and POS are not PPO
  plans, but their members still reach PPO doctors: the PPO network serves **586** plans, **453** as their main network."
- "EPO is not PPO either: a California Department of Insurance filing shows it runs on its own **PureCare** network."

### 1:50–2:15 · Which providers are PPO network? (show: logic §4.1, Provider PPO Evidence sheet)
- "Inside the shared file, **210 provider groups are proven PPO**: they practise in counties where Health Net has **no HMO
  network** (state data: HMO in 31 counties, PPO in all 58). 104 show HMO signals such as capitation, and they're flagged, not
  dropped."
- "The rest are priced **exactly like** the proven PPO providers (median difference +0.011 per code). Every row of the rate file
  carries this evidence level."

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
- "We tested our own rules too. We planned to trust NPPES records updated in the last two years. The data said no: Medicare confirms
  recently updated records 64% of the time and older ones 70%. So that rule was dropped. Statewide, NPPES is the odd one out
  more often than Medicare, but Medicare is wrong 14% of the time too, so we reward **agreement**, never a single source."
- "One TIN can have many addresses: 220 TINs and 189 NPIs have more than one verified or probable site, and every one stays in the file."

### 4:15–4:50 · The rate file, UI and bonus (show: Analyst view, filter 99213; then Excel)
- "Rates for every provider, not just the easy ones: 99.7% of the file's prices are hospital facility prices hidden under
  revenue codes. We decode the procedure code each one carries, so a knee replacement's $41,000 facility fee sits next to
  the surgeon's fee. That takes us from 134 to 203 billing codes."
- "When one provider has several prices for a service, they come from different contracts. We keep the primary contract's
  price, a real contracted rate, not an average nobody is paid. The out-of-network file is never mixed in: it proves which
  contracts the PPO plans do *not* use, and shows in-network prices are about 19x below what is paid out-of-network."
- "One rate per row: the standard, no-modifier office rate. Every variant, like telehealth, is in a second sheet."
- "The analyst view filters by code, specialty and address tier. Cost effectiveness compares providers on the same code, in the same
  specialty and county, weighted by what matters for that specialty."
- "And every rate is checked against Medicare for the provider's own California locality. Health Net's office visit is
  exactly Medicare: $104.88 against $104.89. So when a provider has only one or two codes, we still compare its contract
  level as a percent of Medicare. Every provider gets a label with a confidence. The labels agree with Medicare:
  cost-effective providers sit at 80% of Medicare, high-cost ones at 131%."

### 4:50–5:00 · Close
"Everything re-runs with one command from the README, streams large files, runs in parallel, and passes 55 independent checks plus 35 unit tests,
including a full recomputation of every extracted rate."
