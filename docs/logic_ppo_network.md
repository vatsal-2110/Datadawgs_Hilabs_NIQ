# Logic file — Part A: Identifying Centene's PPO network (C1, C2, C4)

**Scope (from the PS):** Payer Centene · Market commercial group, California · Network PPO · NPI Type 1 and Type 2.
**Bottom line:** 453 plans use the PPO network as their network (393 of them proven by plan design / plan documents,
not by name), another 133 (EOA, POS) reach it through a PPO tier, and all of them are served by **one** rate file
(`2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json`). From it we extracted **7,520 rates** for
**2,824 NPIs / 2,231 TINs** on the HiLabs billing codes.

**How to read this file.** Every decision is backed by evidence that does **not** come only from the payer's own labels
(plan names, file names). Each claim is tagged:
**[data]** read from the Centene files · **[3rd-party data]** a public dataset from another organization (NPPES, US Dept.
of Labor) · **[regulator]** a CMS or state regulator filing · **[payer document]** Health Net's own published document,
quoted · **[inferred]** reasoning, with its basis.
Reproduce: `python -m src.stage1_scope_evidence` → `data/stage1/c1_scope_evidence.txt`;
`python -m src.stage2_network_evidence` → `data/stage2/c2_network_evidence.txt`; `python -m tests.verify_memberA`.

---

## 1. Which plans are California commercial group? (C1)

**What we parsed.** All 11 Centene index files on centene.com (Health Net, Ambetter, Fidelis, QualChoice + 7
QualChoice sub-indexes): 3,275 plan rows, 80 rate-file references → **66 unique rate files** (14 duplicate references
removed by URL without its signed query string). [data]

**How an index is organized.** Plans are grouped into *reporting structures*; every plan in a structure uses the same
rate files. So the scope question is: **which structures are California commercial group?** Health Net's index holds
exactly **two** structures: one with 1,312 plans using `healthnet-ca_*` files, one with 381 plans using `healthnet-or_*`
files. [data]

### 1.1 Four independent tests per structure (not just the word "CA")
A file or plan name saying "CA" is the payer describing itself. We required independent proof:

| Test | Question | Source |
|---|---|---|
| **E1 market** | Does the index declare `plan_market_type = group`? | [data] |
| **E2 providers** | Where do the providers in the structure's rate file actually practise? NPPES practice-location state of **every** NPI (fixed random sample of 400 for files > 5,000 NPIs) | NPPES NPI Registry API [3rd-party data] |
| **E3 employers** | Group plan IDs are **employer EINs**. Where are those employers? Matched to every Form 5500 / 5500-SF filed for plan years 2023–2024 (922,039 sponsor EINs) | US Dept. of Labor, EBSA Form 5500 datasets [3rd-party data] |
| **E4 issuer** | HIOS-format plan IDs start with the 5-digit **issuer ID** assigned by CMS. Which licensed company and state is it? | CMS and state regulator filings [regulator] |

**Rule:** a structure is in scope only if E1 = group **and** every available geographic test (E2–E4) points to California.

### 1.2 Results

| Structure | E1 market | E2 providers (NPPES) | E3 employers (DOL Form 5500) | E4 issuer (regulator filings) | Verdict |
|---|---|---|---|---|---|
| **Health Net, 1,312 plans** | group | **96.6% CA** (3,601 of 3,726 NPIs) | **76.3% CA** (681 of 892 matched EINs) | 154 HIOS plans: **Health Net of California, Inc.** (67138) | **IN SCOPE** |
| Health Net, 381 plans ("Oregon") | group | **97.3% OR** (399 of 410 NPIs) | **99.2% OR** (124 of 125) | Health Net Health Plan of **Oregon**, Inc. (10940) | out: Oregon |
| QualChoice (5 structures, 358 plans) | group | rate file not downloadable here | **100% AR** (28 matched) | QualChoice Life & Health / QCA Health Plan, **Arkansas** (37903, 70525) | out: Arkansas |
| QualChoice (3 structures, 10 plans) | group | — | no EIN matched | — | out: no evidence of CA |
| Ambetter CA (58 plans) | **individual** | 90.7% CA | — | Health Net of California (67138) | out: **individual market**, not group |
| Ambetter other states (27 structures), Fidelis (2) | individual | — | — | each plan's HIOS state: 27 non-CA states; Fidelis = NY Quality Healthcare Corp. (25303) | out: individual market |

**What the "Oregon plans" are.** The second Health Net structure: 381 plans named after **Oregon employers** (e.g.
"COLUMBIA EXCAVATING, LLC", "MCA ARCHITECTS, PC"), with **no state in the name**. A name-based rule can't place them;
the evidence can: their providers practise in Oregon (97.3%), their employers file Form 5500 from Oregon (99.2%), and
their HIOS issuer is Health Net Health Plan of Oregon. Brand-level scoping ("Health Net = California") would have
wrongly added them.

**The reverse question: could a California plan lack "CA" in its name?** Yes, so scope is decided per structure, by
evidence, never by the plan name. The 154 small-group plans with no "CA" prefix ("Gold PPO 500/20", "SmartCare HMO
Gold $30") are in scope because their structure passes E2–E4 and their HIOS issuer is Health Net of California.

**Honest notes.**
- 211 in-scope employers file Form 5500 from another state (NY 31, WA 21, TX 20, …). They are kept: their plan's
  network is the California file (E2); multi-state employers buy California coverage for California staff. [inferred]
- 263 in-scope plan EINs aren't in Form 5500. Small fully-insured welfare plans (< 100 participants) are exempt from
  filing (29 CFR 2520.104-20), so absence is expected, not suspicious. [regulator]
- The automated verdict agrees with the C1 scope flag for **every** structure; C1 was right, and now it is proven.

## 2. The key structural fact: one shared rate file
The 1,312 CA plans list **exactly one** in-network file (701.6 MB) plus one allowed-amounts file. PPO, HMO, EOA, POS and
EPO plans **all** point to the same file. [data] So the PPO network can't be found by "picking the PPO files". It has to
be found at **plan level** (section 3), and then we ask whether the **providers** inside the file can be separated
(section 4).

**Why rates come only from the in-network file.** Every plan lists two files because the Transparency in Coverage
rule requires both: the *in-network* file (negotiated rates with contracted providers) and the *allowed-amounts* file
(historical payments to **out-of-network** providers). The PS asks for the PPO **network's** rates, so only the
in-network file is used; mixing in out-of-network history would compare unlike prices. [regulator: 45 CFR 147.212]

## 3. Which plans use the PPO network (C2): predicted from evidence, not from the plan name

### 3.0 Why the plan name is not enough, and what we did instead
All 1,312 CA plans point to the **same** rate file, the index has no network field, and the rate file's
`network_name` is empty for all 2,825 provider groups; every price entry points to exactly one provider group, so
there is no hidden network grouping in the PTD files either. [data] The only plan-specific network statement in the
PTD data is a word in the plan name ("PPO", "HMO", …): the payer's own label, not proof.

So C2 **predicts each plan's network from facts that do not depend on that word**, using Health Net's own legally
required plan documents, and only afterwards compares the prediction with the name
(`python -m src.stage2_plan_design_evidence` → `data/stage2/c2_plan_design.txt`, sheet *Plan Network Use*).

### 3.1 Evidence D: the plan's benefit design, matched to Health Net's 2026 plan documents
- **What a plan's numbers are.** The numbers in a large-group plan name are its benefit design: copay /
  deductible / coinsurance / out-of-pocket maximum (e.g. "30/500/4000/30%"). [data]
- **Where the official versions are.** Health Net's broker library publishes the official 2026 **plan overview for
  every large-group design** (208 documents) and the **Summary of Benefits and Coverage (SBC)** of every small-group
  plan (242). [payer document] We downloaded all 450 and read what **each document says about itself**:

| Family in Health Net's catalog | Documents | Out-of-network benefit column | HMO / PPO tier columns | Contract the document points to |
|---|---|---|---|---|
| **PPO / HSA-compatible PPO** | 35 | **100%** | 0% | **"Certificate of Insurance"** (an insurance policy) |
| **EOA** (Full Network, ExcelCare) | 64 | 0% | **100%** ("HMO: $40 / PPO: $60") | "Evidence of Coverage" |
| **HMO** (Full Network, ExcelCare, SmartCare, Salud) | 109 | 0% | 0% | "Evidence of Coverage"; Salud: *"Salud con Health Net HMO is offered by Health Net of California, Inc."* |

"Evidence of Coverage" is the contract of a Knox-Keene health care service plan (the HMO-type regime regulated by the
CA Department of Managed Health Care); a "Certificate of Insurance" is an insurance contract. [regulator]

**Finding: no design is offered both as PPO and as HMO/EOA.** Of 67 distinct large-group designs, 34 exist only as PPO,
32 are shared by HMO and EOA (**identical benefit grids**: EOA is the HMO design plus a PPO tier), and 1 is HMO only.
So a plan's numbers alone decide PPO vs HMO family. Two refinements:
- **M, earlier-year designs:** designs at an earlier year's legal out-of-pocket maximum (e.g. $9,450, the 2024
  federal maximum) are matched on copay / deductible / coinsurance only.
- **M2, partial designs:** custom names that print only copay / deductible / coinsurance are matched when that
  combination exists in one family only.

### 3.2 Evidence S, A, N: the plan's own SBC, HSA design, network brand
| Code | Evidence | Why it is independent of the network word |
|---|---|---|
| **S** | the plan's own **Summary of Benefits and Coverage** (small group) states **"Plan Type: PPO"** or **"Plan Type: HMO"** | federally required, standardized disclosure for that exact plan |
| **A** | **HSA-compatible** design (HSA / HSAF / HSAI) | Health Net offers HSA-compatible plans **only** in its PPO catalog |
| **N** | tailored **network brand**: SmartCare, ExcelCare, WholeCare, Salud (name codes SC / EC / SAL read as these brands: inferred) | these networks appear **only** on HMO / EOA plan documents |

### 3.3 What each product type is, and how it uses the PPO network
| Product | Network(s) its members use | Evidence |
|---|---|---|
| **PPO / HSA PPO** | **Health Net PPO Network** (all 58 CA counties; Cigna PPO out of state), in- **and out-of-network** benefits | plan overviews: Certificate of Insurance + out-of-network column; 2026 portfolio guide: *"58 counties statewide (HN proprietary network)"*; member form FRM049776EC01: *"PPO and EPO plans underwritten by Health Net Life Insurance Company … California Department of Insurance"* [payer document, regulator] |
| **EOA** | **HMO network** for most care; **PPO network as a tier** for office-based professional services | EOA flyer FLY1104775EH01w: *"EOA products are available with our full HMO Network or our ExcelCare tailored network"*; *"If a member needs specialist care, surgery or hospital services, the PCP will make arrangements with his or her participating physician group"*; PPO tier = *"self-refer to statewide PPO specialists for exams and evaluations … most procedures that can be performed in the doctor's office only"*; plan overviews: Evidence of Coverage + HMO / PPO tiers [payer document] |
| **POS** | **HMO option + PPO option + out-of-network** | SELECT POS flyer FLY032979EP01: *"Through the HMO option, your primary doctor will coordinate your routine and hospital care … Through the PPO and out-of-network options, you have the freedom to see specialists"*; offered by Health Net of California (DMHC) [payer document] |
| **HMO** | **HMO network only** (31 counties) | plan overviews: Evidence of Coverage, no out-of-network column; plan-type page: *"All plans are tied to our HMO provider network"*; OPA report card: HMO service area 31 counties [payer document, regulator] |
| **EPO** | **PureCare One EPO network**: separate, not the PPO network | CA Department of Insurance network-waiver filing for Health Net PureCare One EPO [regulator] |

**The insight the name hides: the PPO network is used by more plans than the "PPO" plans.** PPO plans use it as their
network; EOA and POS members also reach PPO-network providers through a PPO tier. EOA and POS are still **not PPO plans**:
they're Evidence-of-Coverage HMO-family products where hospital and most specialist care run through an HMO physician
group. But the PPO network's professional rates apply to their PPO-tier visits.

### 3.4 Result (`data/stage2/c2_plan_design.txt`)
| Network use | Plans | From evidence (D / M / M2 / S / A / N, or product document) | Name only* |
|---|---|---|---|
| **PPO_NETWORK_PRIMARY** (PPO, HSA PPO) | **453** | **393** | 60 |
| **PPO_NETWORK_TIER** (EOA 127, POS 6) | **133** | 124 | 9 |
| HMO_NETWORK_ONLY | 723 | 656 | 67 |
| EPO_NETWORK | 3 | 3 | 0 |
| **Total** | **1,312** | **1,176 (90%)** | **136** |

\* *Name only* = employer-custom designs ("CA L CUS PPO SVR Alt 2 Aon NCA", "City of Pleasanton $15") and exchange
products with **no public plan document**; for these the network word is the only plan-specific statement, and they
are marked so.

**Agreement check, done after the prediction:** for the 1,167 plans classified by D / M / M2 / S / A / N, the
prediction **disagrees with the plan-name word 0 times**. The 393 evidence-based PPO plans are all named PPO; no plan
with an HMO / EOA design is named PPO.

**PPO network plans:** 453 use it as their network; **586** plans' members use it (453 + 133 through a tier).
**PPO rate file:** 1 (`…healthnet-ca_in-network.json`), shared by all of them.

### 3.5 Cross-checks from other independent sources (`data/stage2/c2_network_evidence.txt`)
**T2: regulator product codes (CMS HIOS).** The 154 small-group plans' HIOS IDs group them into 9 regulator-filed
products; every product holds a single plan type. Product **67138CA073** holds all 34 small-group PPO plans and nothing
else. [regulator data]

**T3: what employers told the federal government (DOL Form 5500 Schedule A HMO / PPO boxes).**

| Employer's Health Net plans in the index | Declared on Schedule A |
|---|---|
| include a PPO plan (51 employers) | a **PPO** contract: **42 (82%)** |
| only HMO / EOA / POS (35 employers) | an **HMO** contract: **34 (97%)**; PPO-only: 1 |

15 rows (9 employers with both HMO and PPO plans) ticked only one box; listed for review, not changed. [3rd-party data]

### 3.6 Hard cases
| Plan | Decision | Evidence |
|---|---|---|
| "CA L CUS PPO … IRONWORKERS FWP **EPO**$15" and 2 similar | EPO network, not PPO | EPO is the specific product; the same employer also files separate plain PPO and HMO plans; EPO = PureCare One EPO network (CDI) |
| "CA L SAL Mex 5/0/1500" | HMO | design "5/0" = Health Net **Salud Mexico** plan (SIMNSA network), an HMO-family design |
| "CA L SC HMO 60/1500a/9450", "CA L PPO 60/5000/9450/30%" | HMO / PPO | evidence M: same grid as a 2026 design, out-of-pocket at the 2024 legal maximum |
| "CA L PPO HSA ED 30%/3200/5000" | PPO | evidence A: HSA-compatible designs exist only as PPO |
| 2 PPO rows with plan ID `999999999` | PPO | placeholder ID; classified by design |

### 3.7 Name rules: used only for the 136 plans with no public document
The network word is read with a fixed precedence (EOA > POS > EPO > HMO > PPO; Salud family code = HMO), because the
narrower product wins when a name carries two words ("CUS PPO … EPO"). For the 1,176 plans with evidence these rules
were not used to decide, only to measure agreement.

### 3.8 Conflicts and how we resolved them (PPO network, C1 → C6)
One rule throughout: **independent evidence beats the label in the file; when evidence is missing we say so, and we
never drop data silently.** Every conflict below is resolved the same way each run, and the losing value is kept in a
column or sheet so a reviewer can check it.

| # | Conflict | Resolution | Where it is recorded |
|---|---|---|---|
| 1 | The plan **name** says one network, the plan's **benefit design** (Health Net plan documents) says another | Evidence wins; the name is used only when no document exists (60 of 453 PPO plans), and that is labelled `basis = NAME_ONLY`. On the plans with both, evidence and name never disagreed | `plan_labels.csv` (`final_network_use`, `basis`, `name_evidence`) |
| 2 | Name contains both "PPO" and "EPO" (3 plans) | The specific product wins: EPO, its own PureCare One EPO network (CA Dept. of Insurance filing), not PPO | §3.6, `plan_labels.csv` |
| 3 | EOA and POS plans also reach PPO providers (as an out-of-network or second tier) | They are HMO-based products (Health Net's own definition, Evidence of Coverage contract form), so **not PPO plans**: counted separately as `PPO_NETWORK_TIER` (133), never in the 453 | §3.3–3.4 |
| 4 | Plan ID labelled `ein` but formatted as a 10-character HIOS ID (154 rows), or the placeholder 999999999 (3 rows) | The format decides the type; placeholders are classified by plan design only | C1 table `plan_id_format`; C3 index metric "Correct plan IDs" |
| 5 | A Health Net reporting structure looks Californian by brand but its providers and employers are in Oregon | Third-party evidence (NPPES practice states 97.3% OR, employers 99.2% OR, HIOS issuer 10940 Oregon) wins: out of scope | §1, C1 Excel "Scope Evidence" |
| 6 | All CA plans, PPO and HMO, point to the **same** rate file with no network field: which providers are PPO? | Provider-level evidence (§4.1). Providers with HMO-only evidence (paid out-of-network by the PPO plans, capitation, IPA / Ambetter-HMO label) leave the C6 Rate File for the sheet "Excluded - HMO signals" with the reason; providers with a proven PPO sign **and** an HMO sign (MIXED, 3) stay, flagged | `provider_network_evidence.csv`; C6 sheet "Excluded - HMO signals" |
| 7 | One provider, one service, several prices from different contracts | The provider's **primary contract** (the provider_reference that prices the most services): a real contracted price, not an average nobody is paid; all prices kept | §5 step 4; Rate Variants sheet |
| 8 | A professional fee and a facility fee for the same code | Different services, never averaged; the headline uses the provider's own component; C7 compares like with like | §5 step 3; `rate_component` |
| 9 | In-network rate vs out-of-network allowed amount for the same code | Never mixed: the allowed amount is a payment, not a negotiated rate; used only as evidence (#6) and as a benchmark | §5 step 6 |
| 10 | Address sources disagree (NPPES vs Medicare vs registries vs state licence) | Agreement between independent families decides the tier; no single source is trusted alone; every candidate is kept with its tier | `docs/logic_address_mapping.md` §2 (conflicts) |

## 4. Can the providers inside the shared file be split into PPO vs HMO?
Tested from five angles; all say **no**:
1. **No network field.** `network_name` is empty in **2,825 of 2,825** provider groups. [data]
2. **One price per provider per service.** Mixed PPO and HMO contracts would give one provider two prices for the same
   service. Only **0.49%** of 849,594 (service, NPI, TIN, conditions) combinations have more than one price. *Mistake
   caught:* a first run said 43%; the extra prices were sub-codes (`additional_information`), i.e. different services.
3. **Business-name labels don't overlap.** "HEALTH NET DIRECT FFS COMMERCIAL" (277 TINs), "PPO: Los Angeles County"
   (38), "DIRECT NETWORK: AMBETTER HMO" (15), IPAs. Only **6 of 3,734 NPIs** appear under more than one label. [data]
4. **Other Centene CA files don't help.** Ambetter CA fills `network_name` with one value, "Exchange CA". [data]
5. **No public directory to check against.** Health Net's FHIR directory needs a developer-portal login; the public
   directory file found is Medi-Cal only. [payer document]

**Decision D2.7:** the PPO network's rates = the whole CA in-network file, with HMO-style providers **flagged, not
dropped**. All 453 PPO plans name this file; each provider has one price per service, so no PPO price is mixed with an
HMO price. Providers with an HMO-style hint (Ambetter-HMO label, IPA, capitation code) = **97 of 7,520 rate rows
(1.3%)**; results are reported with and without them. The result is an upper bound: a few providers may serve only
HMO/EOA members.

### 4.1 Provider-level PPO evidence (`python -m src.stage2_provider_network` → `data/stage2/c2_provider_network.txt`)
D2.7 is a *default*. We also grade every (NPI, TIN) pair on evidence that does not depend on it:

| Signal | Rule | Why it is evidence | Source |
|---|---|---|---|
| **G — geography** (PPO) | a VERIFIED/PROBABLE practice address in a CA county **outside** Health Net's HMO service area | The state lists the Health Net **HMO** service area as **31 counties** and the Health Net **PPO** service area as **"All of California"**. A Health Net contract with a provider practising elsewhere can only serve members through the PPO network | CA Office of the Patient Advocate report cards 2025-26 [regulator]; Health Net 2026 portfolio guide: HMO "30 counties statewide", PPO "58 counties statewide (HN proprietary network)" [payer document] |
| **L — label** (PPO) | provider group named "PPO: <county>", "HEALTH NET PPO", "AMBETTER PPO" | the payer names the network on the contract | [data] |
| **C — capitation** (HMO) | priced under `negotiation_arrangement = capitation` | in California capitation is an HMO payment model; PPO plans pay fee-for-service | Milliman, *Healthcare under the delegated risk model in California* [3rd-party]; [data] |
| **H — label** (HMO) | group named as an IPA or "DIRECT NETWORK: AMBETTER HMO" | IPAs are the HMO delegated model | [data] |
| **O — paid out-of-network** (HMO) | the PPO plans' own **allowed-amounts file** shows the (NPI, TIN) paid **out-of-network** for a non-emergency service | the pair has a contract in the shared file, yet the PPO plans pay it as out-of-network, so that contract is not a PPO-network contract. Emergency codes / ER place of service are excluded (paid whatever the network). Validated before use: 0 of 73 such pairs had been PPO_CONFIRMED by G/L, and C/H HMO evidence is 4x more common among them (12.3% vs 2.8%) | allowed-amounts file [data] |

| Class | Pairs | Pairs with rates | Meaning |
|---|---|---|---|
| **PPO_CONFIRMED** | **210** | **162** | G (92 pairs: Monterey 17, Butte 17, San Luis Obispo 13, Sutter 9, Humboldt 7, Shasta 7, …) or L (123 pairs), no HMO evidence |
| HMO_SIGNAL | 168 | 135 | C (74 pairs), H (33 pairs) or O (73 pairs), no PPO evidence |
| MIXED | 3 | 3 | both |
| PPO_PRESUMED | 3,399 | 2,737 | direct fee-for-service contract, no further evidence; kept under D2.7 |

**Price signature (independent of the classes' definitions).** On the same code, billing class and place of service
(standard service, codes with ≥ 10 providers), each provider's rate ÷ the code median, compared by class:
PPO_PRESUMED − PPO_CONFIRMED = **+0.011** (median over 30 codes): the presumed providers sit on the **same fee
schedule** as the proven PPO providers. HMO_SIGNAL − PPO_CONFIRMED = **+0.228** (29 codes): HMO-signal contracts are
priced differently. The distributions are not identical in their tails (Mann-Whitney p = 0.006 confirmed vs presumed;
p = 0.0001 confirmed vs HMO), so this is supporting evidence, not proof.

**What this changes.** (1) D2.7 is no longer only an argument from absence: 210 pairs are proven PPO-network
contracts that cannot be HMO contracts, and the presumed majority is priced like them. (2) Every row of the final rate
file carries `ppo_network_evidence`, so a contracting team can keep a strictly proven view (PPO_CONFIRMED) or the full
network view. (3) **Updated 2026-10-18 (D6.5):** the HMO-signal providers are moved **out** of the Rate File into the
sheet "Excluded - HMO signals" (1,442 rows, 135 providers with rates, each with its reason), because the PS asks for PPO-network
rates and these carry evidence of an HMO-only contract. MIXED (a proven PPO sign plus an HMO sign) stays, flagged.

**Context a contracting team should know.** Health Net has announced it is leaving California's commercial group
market, with in-force group coverage ending around February–March 2027 (Healthcare Finance News). These contracts
have a limited remaining life. [3rd-party]

## 5. Rates for the PPO network (C4) (`python -m src.stage4_extract_rates` → `data/stage4/c4_report.txt`)
The task says "extract rates for **every provider**". Reading only the `billing_code` field of professional items gives
just 134 HiLabs codes and 2,824 of the file's 3,734 NPIs, because 99.7% of the file's prices are **facility** prices
filed under revenue codes (RC). We therefore read the whole file and decode what each price is.

**Step 1: extract everything.** Every dollar price of every code type, for every provider → `rates_all.parquet`:
264,439 rows, 1,276 codes, **3,734 of 3,734 NPIs**. `rates.parquet` is its HiLabs-code subset, used by C6/C7.

**Step 2: decode facility prices.** An RC item's price often names a CPT/HCPCS code in `additional_information`
(CPT 529,719 prices, HCPCS 593,684). That price is the hospital's **facility fee for that procedure** (a UB-04 claim
line is revenue code + CPT/HCPCS). We set `billing_code` = the named code and keep the RC as `revenue_code`. The RC
item's own name (e.g. "Emergency Room General") is only the container, so it goes to `source_items`, not into the
service key; otherwise one procedure would appear under 19 item names at the same price. Result: **HiLabs codes priced
134 → 203 (68.8% → 75.6% of HiLabs weight)**. 69 codes exist only as facility prices, e.g. knee replacement 27447
$41,011 (RC 360), knee arthroscopy 29881 $24,895, laparoscopic cholecystectomy 47562 $23,495, upper GI endoscopy 43239
$9,012 (RC 750), and the ER visit 99283 facility fee $3,131 at 119 hospitals. RC prices with no embedded code (room and
board, per diems) stay revenue-code rates in `rates_all`. [data]

**Step 3: what counts as "one service".** NPI · TIN · code · billing class (professional vs facility) · modifier ·
place of service · revenue code · arrangement (FFS vs capitation). These are different services with legitimately
different prices, so they are never averaged together.

**Step 4: several rates for one service: which one we keep, and why (D4.6).** We profiled the duplicates: every
multi-price professional key comes from **different contracts** (`provider_references`) for the same provider, not from
noise inside one contract. So the question is which contract is the provider's network contract. We keep the price in
the **primary contract**: the provider_reference that prices the most services (the group's main fee-schedule
agreement, where narrow carve-out contracts price only a handful of codes). Why not the median: the median of two
contracts ($198 and $692 for 90837) is a price **nobody is paid**, whereas the primary-contract rate is a real contracted
price. Ties, or one contract with several prices, use the median. 1,233 HiLabs rows are priced by more than one contract,
and in 306 of them the primary rate differs from the all-contract median. `rate_median_all`, `rate_min`, `rate_max`,
`n_contracts` and `rate_source` keep the full picture. "Most recent" is impossible: every expiry is 9999-12-31 and there
are no effective dates.

**Step 5: filters.** Dollar types only (negotiated / fee schedule / derived), rate > $0, and expiry judged against the
file's own `last_updated_on` (2026-10-06) rather than today's date, so re-runs give the same answer. Nothing in this file
was dropped by these rules. The 42 capitation rows are kept and flagged, and never chosen as a headline rate while a
fee-for-service price exists.

**Step 6: the out-of-network (allowed-amounts) file is used, but never mixed into rates.** It lists what the PPO plans
paid **non-contracted** providers (1,970 payments, 336 codes, 727 NPIs). An allowed amount is not a negotiated rate, so
it is kept in `oon_allowed.parquet` and used two ways:
(a) **network evidence:** 84 (NPI, TIN) pairs listed in the in-network file were paid out-of-network, so their contract
is not a PPO contract (signal O, §4.1);
(b) **benchmark:** `oon_vs_in_network.parquet` compares, per code and billing class, the in-network median with the
out-of-network allowed median. The median ratio is 18.8x on 56 HiLabs codes (24.3x on all 158 shared codes): 99213
costs $105 in-network vs $793 out-of-network, and ER visit 99285 $475 vs $14,367. This is what the network's negotiated
discounts are worth. Some allowed amounts look like whole-claim totals (venipuncture 36415: $857), which is one more
reason never to treat them as unit rates.

**Step 7: is each rate plausible? Medicare benchmark + robust outliers** (`python -m src.stage4_medicare_benchmark` →
`data/stage4/c4_medicare_benchmark.txt`). Each professional rate is compared with the **2026 Medicare Physician Fee
Schedule for the provider's own California locality**: (work RVU × GPCI + practice-expense RVU × GPCI + malpractice RVU
× GPCI) × conversion factor 33.4009. Facility or non-facility practice expense is chosen by place of service, the 26/TC lines are used for those
modifiers, and the locality comes from the county of the pair's best address (CMS locality–county file). Facility
rates are compared with the OPPS national payment (Addendum B, October 2026 quarterly update). Sources: CMS RVU26D
(October 2026 release) and OPPS Addendum B October 2026 [regulator].
- **Is the Medicare data itself current? (tested, not assumed).** RVU26D is the 4th and latest 2026 quarter (released
  2026-08-26); no later 2026 or 2027 file existed on the run date. We first used the annual OPPS rule (Dec 2025) and
  replaced it with the October 2026 update: procedure rates are identical, and 16 of 85 matched codes changed, all
  quarterly-priced drugs. Independent proof that 2026 is the right year: Health Net's own professional rates match the 2026 schedule
  for the provider's locality and not the 2025 one. Exactly 100% of Medicare: 600 rows (2026) vs 0 (2025). At a
  whole-number % of Medicare, as contracts are written: 1,744 of 3,740 (2026) vs 232 (2025), where chance alone gives about 374.
  The check re-runs on every pipeline run (`c4_medicare_benchmark.txt`). While testing, we found CMS moved the conversion-factor
  column in the 2026 file (a new "PRIC IND" column), so the loader now finds columns by header name.
- 84.3% of HiLabs rate rows get a Medicare price (no Medicare price: drugs (ASP), lab fee schedule, packaged codes).
- **Finding 1: the professional schedule is Medicare-pegged.** 99213 is $104.88 against Medicare LA $104.89, and 99214
  is exactly 100%. 18% of standard professional rates are within 1% of Medicare, and the common contract levels are 80%,
  90%, 100%, 150%, 170% and 360%. Median 120% [p25 100%, p75 271%].
- **Finding 2: facility amounts are not line-item fees.** Their median is 16× OPPS, and within one hospital the ratio
  swings widely (log-sd 0.56). Cheap services carry case-sized prices (foot X-ray 73630 $4,900, pathology 88305 $9,792),
  so they behave like encounter-level prices. They are kept and labelled `facility`, and are never compared with professional
  fees.
- `rate_check` per row: HIGH/LOW_VS_PEERS (robust median/MAD z > 3.5 among the same code, component and modifier;
  714 rows), ABOVE_10X / BELOW_30PCT_MEDICARE (professional only; 167 rows, e.g. ECG read 93010 at $506.84 vs
  Medicare $8.63). Flagged rows are real file values and are kept, but C7 excludes them from peer comparisons.

**Step 8: recover each provider's contract formula** (`python -m src.stage4_contract_terms` →
`data/stage4/provider_contract_terms.csv`, `c4_contract_terms.txt`). Contracts on a Medicare-pegged schedule read "pay
X% of Medicare", but the file only carries the dollars. For each provider we divide every professional rate by its
own-locality Medicare price and find the percentage most of its codes agree on (±1%). EXACT means ≥ 2 codes with ≥ 80%
of them on it; LIKELY ≥ 50%; SINGLE means one code; NONE means no shared multiplier. A provider with SINGLE/NONE inherits an
EXACT formula from the other NPIs of its TIN (group contracts). Result: 2,570 providers. EXACT 349 (+252 inherited from
their TIN), LIKELY 389, SINGLE 1,583, NONE 249. Of the 601 EXACT formulas, 429 sit at a whole-number % of Medicare.
Common levels are 100% (167 providers), 95%, 84%, 90%, 70%. 1,817 of 2,503 professional rates fit their provider's
formula to ±1%.

**Step 9: estimate the missing rates, only where a test proves it works** (`data/stage4/estimated_rates.csv`, never mixed
with real rates). For a provider with an EXACT formula, each HiLabs code of its specialty that the file does not price
for it is estimated as multiplier × Medicare (non-facility, own locality). **Hide-and-check test (leave-one-out):** each
known rate is hidden, the formula refit on the other codes and the hidden rate predicted.

| formula used | tests | within 1% | within 5% | shipped |
|---|---|---|---|---|
| provider's own EXACT | 359 | 85.2% | **88.6%** | yes |
| TIN's EXACT (fit without the provider) | 290 | 84.8% | **96.6%** | yes |
| own LIKELY / SINGLE / NONE | 3,181 | 17–46% | 22–59% | **no** |
| baseline: statewide median of the code | — | — | 28.6% | — |

So 4,664 estimated rates (445 providers, 269 codes, confidence HIGH 3,151 / MEDIUM 1,513) are shipped. Formulas that
failed the test produce no estimates.

**Result:** 8,820 HiLabs rate rows · 2,995 NPIs · 2,356 TINs · 3,037 NPI–TIN pairs · 203 codes (professional: 7,547
rows, 134 codes; facility: 1,273 rows, 120 codes, 176 NPIs). Office visit 99213 $104.88 and 99214 $188.24 (medians
across providers). An independent `json.load` recompute in `tests/verify_memberA.py` reproduces every rate,
primary-contract choice and contract count.

## 6. Data-quality findings handed to grading (C3)
- 154 plan IDs labelled "ein" are really HIOS IDs; 3 rows use placeholder `999999999`; one plan ID carries up to 144 plan names.
- Rate file: 99.7% facility prices; about 17% individual NPIs; `network_name` always empty; expiry always 9999-12-31.
- 7 NPIs in the rate file are **deactivated** in NPPES and 1 doesn't exist: a stale-contract signal.

## 7. External sources (read 2026-10-17/18) and what each proves
| Source | Used for |
|---|---|
| Centene price transparency page: https://www.centene.com/price-transparency-files.html | the index files (C1) |
| California Dept. of Public Health, Licensed and Certified Healthcare Facility Listing (data date 2026-10-16): https://data.chhs.ca.gov/dataset/healthcare-facility-locations | C5: state licensing address of facilities, matched by NPI (4th independent source family) and referee of the tier-accuracy audit |
| CMS Provider Data Catalog: MIPS Overall Performance PY2024 (a174-a962), Hospital General Information (xubh-q36u): https://data.cms.gov/provider-data/ | C7: quality (MIPS score, hospital stars) for the value label |
| CMS 2026 Physician Fee Schedule RVU file, October release (RVUs, CA GPCIs, locality–county map): https://www.cms.gov/files/zip/rvu26d.zip | C4 step 7: Medicare price of every professional rate for the provider's own CA locality; C7 Medicare-normalised index |
| CMS OPPS Addendum B, October 2026 quarterly update: https://www.cms.gov/files/zip/october-2026-opps-addendum-b.zip | C4 step 7: Medicare outpatient-hospital price for facility rates |
| CMS 2025 PFS RVU file (October): https://www.cms.gov/files/zip/rvu25d.zip | C4 step 7: data-currency test only (Health Net fits 2026, not 2025) |
| NPPES NPI Registry API: https://npiregistry.cms.hhs.gov/api/ | E2: where each file's providers practise |
| US DOL EBSA Form 5500 / 5500-SF / Schedule A datasets 2023–2024: https://www.askebsa.dol.gov/FOIA%20Files/ | E3: employer state; T3: employer-declared HMO / PPO contracts |
| CMS HCRP audit report, Health Net of California, HIOS 67138: https://www.cms.gov/files/document/ppfmg-ea-healthnetcalifornia-67138-2019-hcrp-5cr-013025.pdf | E4: issuer 67138 = Health Net of California, Inc. |
| Oregon DFR HIOS plan list: https://dfr.oregon.gov/healthrates/Documents/rhea-quarterly-plans/rhea-plan-list-q4-2021.pdf | E4: issuer 10940 = Health Net's Oregon plans |
| Arkansas Insurance Dept. / CMS rate filings: https://www.insurance.arkansas.gov/site/assets/files/4098/7_qualchoice_individual_on-off_exchange_filing_quac-133639093_final.pdf ; https://ratereview.healthcare.gov/files/2069817_70525_20210802_2022_AR_Indiv_AM_Redacted.pdf | E4: issuers 37903, 70525 = QualChoice / QCA, Arkansas |
| NY DFS rate manual: https://myportal.dfs.ny.gov/documents/538523/20507878/NYQHC_IND_BOTHX_FCNY-132377234_Appr_RateMan.pdf | E4: issuer 25303 = Fidelis Care, New York |
| Health Net member form FRM049776EC01: https://sfhss.healthnetcalifornia.com/content/dam/centene/healthnet/pdfs/member/ca/comm_claim_form_ca_eng.pdf | 3.1: which company and regulator for HMO / HSP / EOA / POS vs PPO / EPO |
| Health Net EOA broker flyer (2026 edition): https://www.healthnet.com/content/dam/centene/healthnet/pdfs/broker/ca/large/fb/2026/hn-broker-eoa-flyer-2026.pdf | 3.1: EOA = HMO network, offered by Health Net of California |
| Health Net large-employer plan types: https://www.healthnet.com/content/healthnet/en_us/find-a-plan/large-employers/type-of-plans.html | 3.1: "EOA is an HMO plan"; HMO plans "tied to our HMO provider network" |
| CA Dept. of Insurance company profile, NAIC 66141: https://interactive.web.insurance.ca.gov/companyprofile/companyprofile?event=companyProfile&doFunction=getCompanyProfile&naic=66141 | 3.1: Health Net Life Insurance Company is a CDI-licensed insurer |
| CA Dept. of Insurance PureCare One EPO network waiver: https://www.insurance.ca.gov/01-consumers/110-health/10-basics/upload/Network-Waiver-Health-Net-SBG-PureCare-One-EPO-PF-2018-01437-2.pdf | 3.1: EPO runs on its own network |
| DMHC enforcement filing, license 933-0300: https://wpso.dmhc.ca.gov/enfactions/docs/4956/1764782251001.pdf | 3.1: Health Net of California is a DMHC-licensed health plan |
| Health Net large-group broker library (2026 plan overviews, 208 documents): https://www.healthnet.com/en_us/brokers/forms-brochures/large-group.html | 3.1: each design's product, contract (Certificate of Insurance / Evidence of Coverage), out-of-network column, HMO/PPO tiers |
| Health Net small-group broker library (Summaries of Benefits and Coverage, 242 documents): https://www.healthnet.com/en_us/brokers/forms-brochures/small-group.html | 3.2: "Plan Type: PPO / HMO" for each small-group plan |
| Health Net SELECT POS flyer FLY032979EP01: https://www.healthnet.com/content/dam/centene/healthnet/pdfs/broker/ca/general/pos-flyer-eng.pdf | 3.3: POS = HMO option + PPO option + out-of-network |
| CA Office of the Patient Advocate report card, Health Net HMO 2025-26: https://reportcard.opa.ca.gov/rc/profile.aspx?EntityType=HMO&Entity=HEALTH_NET | 4.1: HMO service area = 31 counties |
| CA Office of the Patient Advocate report card, Health Net PPO 2025-26: https://reportcard.opa.ca.gov/rc/profile.aspx?EntityType=PPO&Entity=HEALTH_NET_PPO | 4.1: PPO service area = all of California |
| Health Net Large Group 2026 Portfolios guide: https://www.healthnet.com/content/dam/centene/healthnet/pdfs/broker/ca/large/fb/2026/ec-portfolio-guide-2026.pdf | 3.1 / 4.1: PPO network "58 counties statewide (HN proprietary network)", HMO "30 counties"; EOA uses the PPO network only "for certain professional services" |
| Milliman, Healthcare under the delegated risk model in California: https://www.milliman.com/en/insight/healthcare-under-the-delegated-risk-model-in-california-lower-cost-high-quality | 4.1: capitation = HMO payment model in California |
| Healthcare Finance News, Health Net exits commercial group market: https://www.healthcarefinancenews.com/news/centenes-health-net-exits-commercial-group-market-2-states | 4.1: contract lifetime context |
| Health Net Salud HMO y Más (large group): https://www.healthnet.com/content/healthnet/en_us/find-a-plan/large-employers/type-of-plans/tailored-networks/saludconhealth.html | 3.4: SAL family = HMO |

*Full decision log with every alternative and limitation: `docs/logic_memberA.md`. Column definitions: `SCHEMAS.md`.*
