# Logic file — Part A: Identifying Centene's PPO network (C1, C2, C4)

**Scope (from the PS):** Payer Centene · Market commercial group, California · Network PPO · NPI Type 1 and Type 2.
**Bottom line:** 453 PPO plans, all served by **one** rate file. From it we extracted **7,520 rates** for **2,824 NPIs / 2,231 TINs** on the HiLabs billing codes.

Every number below comes from the run and is re-checked by `python -m tests.verify_memberA` (23/23 checks pass, including an independent recomputation of all 7,520 rates). Each claim is marked **[data]** (read from the files), **[source]** (an official document, quoted) or **[inferred]** (reasoning, basis stated).

---

## 1. From index files to the California commercial plans (C1)

**What we parsed.** All 11 Centene index files listed on centene.com (Health Net, Ambetter, Fidelis, QualChoice + 7 QualChoice sub-indexes). Each local copy matches the server's byte size. [data]

| | Count |
|---|---|
| Plan rows (plan × reporting structure) | 3,275 |
| Rate-file references | 80 |
| **Unique rate files** (duplicates removed by URL without query string) | **66** (33 in-network + 33 allowed-amounts) |
| Duplicates removed | 14 (all QualChoice: 7 sub-indexes point to the same 2 master files) |

Output: `data/stage1/c1_plan_file_table.csv` with brand, plan name, plan ID, market type and rate file URL, plus the scope flag and its reason. Nothing is deleted; every exclusion carries a reason.

**How each brand and state was ruled out, using the data itself:**

| Group | Plan rows | Why excluded |
|---|---|---|
| Ambetter | 1,120 | `plan_market_type = individual` on every row. 58 rows *are* Californian (HIOS `67138CA…`), so they're excluded by market, not by state [data] |
| Fidelis | 94 | individual market; HIOS state **NY** (88 rows) [data] |
| QualChoice | 368 | no California signal; HIOS state **AR** (85 rows) [data] |
| Health Net – Oregon | 381 | rate files named `healthnet-or_…`, HIOS state **OR**, employer names, no CA signal [data] |
| **Health Net – California** | **1,312 in scope** | group market + CA evidence (file token `-ca_`, names starting "CA", HIOS state CA) [data] |

> **Trap avoided:** "Health Net = California" is not true at brand level. The Health Net index also carries an Oregon structure. Brand-only scoping would have silently added 381 Oregon plans.

---

## 2. The key structural fact: one shared rate file

The 1,312 CA plans form **one** reporting structure that lists **exactly one** in-network file, `2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json` (701.6 MB), plus one allowed-amounts file. [data]

PPO, HMO, EOA, POS and EPO plans **all** point to this same file. So the PPO network can't be found by "picking the PPO files". It has to be found at **plan level** (which plans are PPO), and then we ask whether the **providers** inside the file can be separated (section 4).

---

## 3. Which plans are PPO, and how HMO, EPO and POS were ruled out (C2)

### 3.1 Evidence used
- **Plan names** (477 distinct names across 1,312 rows). Pattern: `CA L [CUS] <family code> <network word> <cost-sharing>`, e.g. "CA L SC HMO 40/500d/4500". [data]
- **Health Net's own documents**, read directly on 2026-10-17:
  - *"Elect Open Access (EOA) is an HMO plan…"*; *"Choose between two tiers of benefits – HMO or PPO"* (Health Net large-employer plan types page). [source]
  - EOA flyer: members *"Choose a participating physician group (PPG)… Then choose a primary care physician (PCP)"*; the PPO option lets them see PPO specialists *"for exams and evaluations without referrals"*. [source]
  - The same page lists the tailored **HMO** networks *"SmartCare, ExcelCare, and Salud HMO y Más"*, and mentions **EPO 0 times and POS 0 times**. [source]

### 3.2 Rules (applied in this order; the first match wins)

| # | Rule | Label | Why this order |
|---|---|---|---|
| R1 | name has **EOA** | EOA hybrid (not PPO) | EOA has both tiers; Health Net defines it as an HMO plan |
| R2 | name has **POS** | POS (not PPO) | explicit |
| R3 | name has **EPO** | EPO (not PPO) | the more restrictive word wins, even when "PPO" is also present |
| R4 | name has **HMO** | HMO (not PPO) | explicit |
| R5 | name has **PPO** | **PPO** | explicit |
| R6 | no network word, but an HMO family code | HMO (medium confidence) | see the hard cases |
| R7 | nothing | UNKNOWN | — (0 plans) |

### 3.3 Hard cases and how they were settled

| Plan name | Decision | Reasoning |
|---|---|---|
| "CA L CUS PPO SACRAMENTO CITY USD- EPO", "… IRONWORKERS FWP EPO$15", "…FWP-EPO$10" | **EPO** product; provider network = PPO network **[inferred]** | Two words, two facts: *EPO* = the benefit rules, *PPO* = the doctors. The same employer ID also files a separate plain PPO/HMO (SCUSD 946002491: "CUS HMO", "CUS PPO", "CUS PPO – EPO") [data]. A Health Net EPO handbook says its EPO *"gives you access to our full PPO network"* [source]. It dates from 2009 and is for another employer, so the network part is **inferred**, not proven. |
| "CA L SAL Mex 5/0/1500", "CA L&T CUS SAL CEMENT MASONS…" (4 rows) | HMO, medium | No network word, but in the SAL (Salud) family, and all **163** other SAL rows say HMO [data] |
| "CA L CUSPPO 15/250/20% RAP" | PPO | Missing space; other employers file "CA L CUS PPO 15/250/20% RAP" [data] |
| 2 PPO rows with plan ID `999999999` | PPO by name | Placeholder ID, never used as a key [data] |
| EOA (127 rows) | **not** PPO | Health Net: *"an HMO plan"*; PCP via a physician group; PPO tier limited to specialist exams [source]. Kept as its own label, so the decision is reversible in one line |

### 3.4 Result

| Product type | Plan rows | Provider network | In the PPO count? |
|---|---|---|---|
| **PPO** | **453** (133 names) | PPO network | **Yes** |
| EPO | 3 | PPO network [inferred] | No: ruled out as an EPO product |
| HMO | 723 | HMO networks | No |
| EOA hybrid | 127 | HMO network + PPO specialists for exams | No |
| POS | 6 | not documented | No |
| Unknown | 0 | — | — |

**PPO plans = 453. PPO rate file = 1** (`…healthnet-ca_in-network.json`). If the 3 EPO plans are counted on their inferred PPO network: 456.

---

## 4. Can the providers inside the shared file be split into PPO vs HMO?

We tested this from five independent angles. All five say **no**:

1. **No network field.** `network_name` is empty in **2,825 of 2,825** provider groups. [data]
2. **One price per provider per service.** If PPO and HMO contracts were mixed, the same provider would often carry two prices for the same service. Only **0.49%** of 849,594 (service, NPI, TIN, conditions) combinations have more than one price. Even when we wrongly merge different services that share a billing code, the figure is at most 2.64%. [data]
   - *Mistake caught:* the first run said 43%. Inspection showed the extra prices were **sub-codes** (`additional_information`, e.g. home-health severity groups), i.e. different services, not different networks. Fixed and logged.
3. **Business-name labels don't overlap.** Some provider groups carry labels in place of a name: "HEALTH NET DIRECT FFS COMMERCIAL" (277 TINs), "PPO: Los Angeles County" (38), "DIRECT NETWORK: AMBETTER HMO" (15), IPAs. These were found by counting names shared across many unrelated TINs. But only **6 of 3,734 NPIs** appear under more than one label, so the groups don't overlap and can't be compared or clustered. Their meaning isn't documented. [data]
4. **Other Centene CA files don't help.** The only CA file that fills `network_name` (Ambetter CA, marketplace) uses one value, "Exchange CA", for all 3,141 provider groups. [data]
5. **No public directory to check against.** Health Net's FHIR provider directory needs a login on Centene's developer portal. The only public Health Net directory file found is Medi-Cal (Medicaid), not commercial. [source]

### Decision D2.7: the PPO network's rates = the whole CA file, with HMO-style providers flagged, not dropped
- **Why:** all 453 PPO plans name this file as their in-network rate file, so every PPO rate is in it. Each provider has one price per service, so no PPO price is mixed with an HMO price.
- **Alternatives:** drop labelled providers (rejected: the labels' meaning is undocumented); a manual Health Net directory check (recommended as validation).
- **Size of the doubt:** providers with an HMO-style hint (Ambetter-HMO label, IPA, or a capitation code) = **97 of 7,520 rate rows (1.3%)**. We report the results with and without them (7,423 rows / 2,795 NPIs without).
- **Limitation:** the result is an **upper bound**. A few providers may serve only HMO/EOA plans.

---

## 5. Rates for the PPO network (C4)

**Codes:** the HiLabs "Specialty to Billing Code" sheet lists 583 codes. **134 are priced in the file (23.0%; 68.8% by HiLabs weight)**, all as professional prices. [data]

**Which rate we keep:**
1. Only HiLabs codes. Only dollar amounts (negotiated / fee schedule / derived). Drop $0 and expired rates. *(This file has no $0 rates, no percentage or per-diem rates, and every expiry date is 9999-12-31, so 0 prices were dropped by these rules.)*
2. One row per **NPI · TIN · code · item description · billing class · modifier · place of service · sub-code**. The item description is needed because one billing code can carry different services (revenue code 250 = different drugs).
3. Several prices for one row (381 rows) → keep the **median**. Store min, max, count and max/min spread. *"Most recent" is impossible* (no effective dates; all expiry dates are 9999-12-31). The median resists outliers such as $105 vs $855 for the same office visit (42 rows have a spread above 3×, flagged for grading).

**Result:** 7,520 rate rows · 2,824 NPIs · 2,231 TINs · 2,865 NPI–TIN pairs · 134 codes. Typical values: office visit 99213 median $104.88, 99214 median $190.27.

---

## 6. Data-quality findings handed to grading (C3)
- 154 plan IDs labelled "ein" are really HIOS IDs; 3 rows use placeholder `999999999`; 6 Fidelis HIOS IDs carry a non-state "EP"; one plan ID carries up to 144 plan names.
- Rate file: 99.7% facility prices; about 17% individual NPIs (big groups like UC Regents and Cedars-Sinai appear with only a few NPIs); `network_name` always empty; expiry always 9999-12-31; the same billing code repeated across items.
- HiLabs code coverage: 23.0% by count, 68.8% by weight.

## 7. Sources (read 2026-10-17)
- Centene price transparency files: https://www.centene.com/price-transparency-files.html
- Health Net, large-employer plan types: https://www.healthnet.com/en_us/find-a-plan/large-employers/type-of-plans.html
- Health Net EOA flyer (5/20): https://www.healthnet.com/content/dam/centene/healthnet/pdfs/broker/ca/general/EOA_flyer_eng.pdf
- Health Net EPO member handbook (Northrop Grumman, 2009): https://www.healthnet.com/static/custom/unprotected/pdfs/CA58857_Northrop_handbook.pdf
- CMS NPI Registry API (NPI type and taxonomy samples): https://npiregistry.cms.hhs.gov/api/
- Centene developer APIs (directory behind login): https://apis.io/providers/centene/ ; Medi-Cal-only public directory noted at https://github.com/jcl347/AccessMediCal

*Full decision log with every alternative and limitation: `docs/logic_memberA.md`. Column definitions: `SCHEMAS.md`.*
