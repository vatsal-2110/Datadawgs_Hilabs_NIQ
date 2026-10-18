# Logic log — Member A (C1 index parsing, C2 PPO identification, C4 rate extraction)

Format for every decision: **Decision → Alternatives → Why → Limitation**. Evidence is quoted from the real files.

> **Status (2026-10-18): this is the working log. The final C1 / C2 logic is `docs/logic_ppo_network.md`.** Entries marked
> **UPDATED** below were superseded by stronger evidence added later (third-party scope tests, plan-design /
> plan-document classification, provider-level PPO evidence). Nothing below was deleted, so the reasoning trail stays visible.

---

## C1 — Index parsing

### What C1 delivers
`data/stage1/c1_plan_file_table.parquet` (+ `.csv`): one row per unique (plan, rate file), with brand, plan name, plan ID, market type, rate file URL, the in-scope flag and the reason. Supporting tables: `plans`, `files`, `plan_file_map`, `index_files` (see `SCHEMAS.md`). Report: `data/stage1/c1_report.txt`.

### Evidence (11 index files, downloaded 2026-10-17 from https://www.centene.com/price-transparency-files.html)
- All 11 headers: entity "Centene Management Company LLC", type "Third Party Administrator", version 2.0.0. Health Net updated 2026-09-30, all others 2026-09-28.
- 3,275 plan rows → 3,267 unique plans → 1,312 in scope (all Health Net CA).
- 80 file references → **66 unique rate files** (33 in-network + 33 allowed-amounts) → **2 in scope**: `2026-09-30_centene-management-company-llc_healthnet-ca_in-network.json` (701.6 MB) and its allowed-amounts file.

| Brand | Plan rows | In scope | Market type in data | States/segments in rate-file names |
|---|---|---|---|---|
| Health Net | 1,693 | 1,312 | group (all) | CA, **OR** |
| Ambetter | 1,120 | 0 | individual (all) | 28 states incl. **CA** |
| Fidelis | 94 | 0 | individual (all) | segments ES, EX (not states) |
| QualChoice (+7 sub-indexes) | 368 | 0 | group (all) | none (single `qualchoice_` file) |

### D1.1 One row per (index, reporting_structure, plan); nothing dropped
- **Alternatives:** dedupe plans by name or by ID at parse time.
- **Why:** neither is a key. 503 plan IDs carry more than one plan name (up to 144), because one employer EIN buys many plans. 331 plan names appear under more than one ID (up to 30), because one product is sold to many employers. Keeping raw rows with a scope flag and reason keeps every exclusion auditable.
- **Limitation:** counts must say which grain they use (plan rows, unique plans, or distinct names).

### D1.2 Logical plan key = brand + normalized name + plan ID + market type
- Normalization: upper-case, single spaces, no spaces around `-` and `/`.
- **Evidence:** "COFS - $1250 Deductible" vs "COFS -$1250 Deductible"; "Out of Area- PreferredChoice $1000 Deductible" vs "OUT OF AREA-PreferredChoice $1000 Deductible" (QualChoice). 1 exact duplicate row inside one structure ("PreferredChoice $500 Deductible", ID 70525).
- **Alternatives:** raw name (would double-count); fuzzy matching (risks merging real variants like "$1000" and "$2000").
- **Why:** it only removes formatting noise and never touches numbers or words.
- **Limitation:** real typos ("Deductable") would not merge. Raw spellings are kept in `plan_name_variants`.

### D1.3 File de-duplication key = sha1(scheme + host + path); query string dropped
- **Alternatives:** full URL; file name only; content hash.
- **Why:** signed URLs differ only by expiring tokens in the query string. A file name alone could collide across hosts. Content hashes would need multi-GB downloads.
- **Evidence:** all 14 removed duplicates are the master QualChoice in-network and allowed-amounts files, referenced by all 8 QualChoice index files (7 sub-indexes × 2 files = 14). No URLs differ only by query string in this release.
- **Limitation:** two genuinely different files at the same path with different query parameters would merge.

### D1.4 Byte-identical index files are recorded, not dropped
- **Evidence:** `qualchoice_big_four.json` and `qualchoice_city_of_fort_smith.json` have the same sha256 (both list the 4 "COFS" plans, i.e. City of Fort Smith).
- **Why:** provenance stays complete (`index_files.identical_to`). Their plans collapse to the same `plan_key`, so nothing is double-counted in the C1 table.

### D1.5 CA scope needs CA evidence on the plan or its files; brand alone is NOT enough
- **Rule:** in scope = brand in scope (config) AND `plan_market_type = group` AND at least one CA signal AND no non-CA state token in the structure's file names.
  - CA signals: `file_state:CA` (the `-ca_` token in the rate-file name), a plan name starting `CA`, or HIOS characters 6–7 = `CA`.
- **Alternatives:** treat every Health Net plan as CA. The first version of the code did this, based on the Centene page note.
- **Why:** the real index showed Health Net also publishes **Oregon**: 381 plans named after small employers ("MCA ARCHITECTS, PC", "VIP HOSPITALITY GROUP - PPO2000 - NN"), files `healthnet-or_*`, HIOS state OR. Brand-only scoping would have pulled them in silently.
- **Only real US state codes count as states.** Fidelis files use `fidelis-es` and `fidelis-ex`, stored as `file_segments`. *Hypothesis, not verified:* ES = Essential Plan, EX = Exchange.
- **Limitation:** relies on Centene's file-naming convention. A future file without a state token falls back to the plan-name and HIOS signals.

### D1.6 How each out-of-scope brand is ruled out (data first, page note second)
- **Ambetter (1,120):** every row is `plan_market_type = individual`. **58 Ambetter plans really are Californian** (`ambetter-ca` file, HIOS `67138CA…`, e.g. "Silver 70 Ambetter PPO AI-AN"). They are excluded by market type, not by state. This matches the Centene note "MHN is now included in Ambetter CA for marketplace plans and in Healthnet for commercial plans".
- **Fidelis (94):** individual market, issuer "Fidelis Care" (New York). No CA signal.
- **QualChoice (368):** group market, but no CA signal anywhere. Issuers "Qualchoice, LLC" and "QualChoice Health Insurance" (Arkansas). Employer sub-indexes: City of Fort Smith, City of Hot Springs, Dillard's, and others.
- **Health Net OR (381):** group market, but the files say OR and there is no CA signal.

### D1.7 Plan-ID format is derived from the value, not trusted from `plan_id_type`
- **Evidence:** 154 Health Net rows say `plan_id_type = "ein"` but hold HIOS IDs (`67138CA073`, `67138CA060`). 3 rows use the placeholder `999999999` ("CA L PPO HSA ED 20%/5000/6000", "CA L PPO 30/3000/5000/30%", "CA L SC HMO 20/20%/2500"). 223 QualChoice rows have 5-digit IDs (`70525`), i.e. a HIOS issuer ID only.
- **Why:** C2 and C3 need to know which IDs are trustworthy. Placeholders must never be used as a join key.
- Flags: `plan_id_format` ∈ {EIN, HIOS, HIOS_ISSUER, PLACEHOLDER, OTHER, MISSING}, plus `id_type_mismatch`. These feed the C3 data-quality metrics.

---

## C2 Step 0 — Profile of the CA in-network file (facts only)
Source: `data/raw/rates/healthnet-ca_in-network.json` (701,615,514 bytes, same size as on the server; header last_updated_on 2026-10-06). Script: `src/stage2_profile_rate_file.py`. Output: `data/stage2/profile_healthnet-ca_in-network.{json,txt}`.

| Fact | Value |
|---|---|
| Provider groups (`provider_references`) | 2,825 (all used); 854 have exactly the same members as another group |
| Unique NPIs / TINs / (NPI,TIN) pairs | 3,734 / 2,888 / 3,775 (5 invalid NPI values) |
| `network_name` filled | **0 of 2,825** (always `[]`) |
| Billing codes (`in_network` items) | 1,717: RC 1,125, CPT 420, HCPCS 172 |
| Price blocks / prices | 397,726 / 1,500,288 |
| `negotiation_arrangement` | ffs 1,684, **capitation 33** |
| `negotiated_type` | negotiated 1,500,242, fee schedule 46 |
| `billing_class` | **institutional 99.7%** (1,496,307), professional 3,981 |
| `setting` | outpatient 100% |
| `expiration_date` | 9999-12-31 for 100% (no real expiry info) |
| $0 rates | 0 |
| Rate quantiles | p01 $74.86, median $7,206.68, p99 $48,995.63, max $327,774.30 |
| `additional_information` | present on 75% of prices: sub-codes such as home-health HIPPS `1EA11`, `3CA11` |

**Business names carrying network labels** (provider-level evidence, free and in the file):
- "PPO: <County>" (89 groups, e.g. "PPO: Los Angeles County", "PPO: Marin County")
- "HealthNet PPO (California)"
- "Direct Network: San Diego", "DIRECT NETWORK: AMBETTER PPO", "DIRECT NETWORK: AMBETTER HMO"
- 13 IPAs: "AllCare IPA", "Allied Pacific of California IPA", "Santa Clara County IPA (SCCIPA)", "EMPIRE HEALTHCARE IPA", and others

### D2.0 The deciding test: does one provider get several prices for the same code under the same conditions?
- **Conditions** = negotiated_type, billing_class, modifiers, service_code (place of service), **and additional_information**.
- **First run (wrong):** without additional_information, 43% of combos looked like they had several prices. Inspecting the raw file showed these were sub-codes (home-health severity groups), i.e. different services, not different contracts. Fixed.
- **Corrected result:** of 849,594 (code, NPI, TIN, conditions) combos, only **4,195 (0.49%)** have more than one price. Only **3,114 (0.37%)** have prices coming from different provider groups. Hospitals appear in many provider groups (355,463 combos), but **almost always at the same price**.
- **Cross-item check (audit):** 116 of 775 billing codes appear in more than one item (e.g. RC 23 in 76 items, RC 250 in 25). Those items are **different services** under one code (RC 250: "STERILE WATER/SALINE, 500 ML", "UNCLASSIFIED DRUGS", …). Even merging them by code alone, only 940 of 35,592 (code, NPI, TIN, conditions) combos (2.64%) have more than one price. That is an upper bound; the within-item figure (0.49%) is the correct one. **Consequence for C4:** the service key must include the item `name`/`description`, not just the billing code.
- **Meaning:** the file is essentially **one price list per provider**. Provider groups are not separate network contracts with separate prices. So price-based clustering cannot split PPO from HMO inside this file. This is the "honest fallback" case from the plan.
- **What remains possible for provider-level separation:** the business-name labels ("PPO: <County>", IPAs, "Direct Network: Ambetter HMO/PPO") and the 33 capitation codes.

### Other Step 0 findings (feed C3 / C4)
- **Coverage is facility-heavy:** 99.7% institutional prices and only 3,734 NPIs. A statewide PPO network has far more physicians, so professional (doctor) fee schedules are largely absent from this file.
- **Duplicate items:** the same billing code (e.g. RC "23") appears as several items with different descriptions but identical prices.
- **Staleness can't be read from expiration_date:** it's always 9999-12-31.

## Hand-off to Member B: `tin_npi_pairs.parquet`
Script `src/stage4_tin_npi_pairs.py`. Result: 3,780 rows = 3,775 (NPI, TIN) pairs + 5 TIN-only rows; 3,734 NPIs, 2,888 TINs.
- 32 NPIs bill under more than one TIN. 244 TINs have more than one NPI (max 80).
- Pairs by billing class: professional 3,186, institutional 580, both 14. 74 pairs are priced under a capitation code.

### D4.0 Send provider pairs before rates
- **Alternatives:** wait for the full C4 extraction.
- **Why:** B only needs who (NPI + TIN), not prices. Reading the provider groups takes seconds and unblocks C5 early.
- **Limitation:** it covers every provider in the shared CA file. The PPO/HMO split (C2) is carried as label columns, not as a filter, so B can map addresses now and we filter later.

### D4.1 TIN-only contracts are kept (NPI = null), not dropped
- **Evidence:** 5 provider groups list NPI `0` (e.g. "Care Fast Urgent Care", "GARCIA, PYS.D.", 2× "HEALTH NET DIRECT FFS COMMERCIAL").
- **Why:** nothing is dropped silently. A TIN-only contract can still be mapped to addresses by TIN.
- **Limitation:** these rows can't reach NPI level without another source.

### D4.2 Low provider coverage is a property of the file, not a parsing gap (checked after Member B's review)
Member B saw that big CA groups appear with only a few NPIs (UC Regents, CEP America and Cedars-Sinai vs thousands of clinicians in Medicare data) and 665 individual (Type 1) NPIs overall. Checks on the raw file:
- **Remote provider lists:** 0 of 2,825 `provider_references` use a remote `location` URL. All 2,825 carry inline `provider_groups`. 0 `negotiated_rates` blocks carry inline groups. Nothing external to download.
- **Before de-duplication:** 4,576 provider_groups holding 5,672 NPI entries → 3,734 unique NPIs.
- **Whole file parsed:** 1,717 `in_network` items, last billing_code `ZZZZZ`, 701,615,514 bytes = server Content-Length, and the file ends with a proper closing `}`.
- **Other parts in the index:** the CA structure (1,312 plans) lists exactly **1** in-network file (plus 1 allowed-amounts file). No split parts.
- **Raw-file examples:** UC Regents TIN 680344702 → 21 NPIs (professional, ~44 codes each); CEP America TIN 942494000 → 56 NPIs; Cedars-Sinai → 5 NPIs over 4 TINs, mostly institutional.
- **Conclusion:** Health Net's CA file lists mostly organization- or facility-level NPIs, so it under-represents individual clinicians. This feeds C3's provider-coverage metric (e.g. file NPIs per TIN vs CMS DAC clinicians per TIN). It is not something C4/C5 can fix from this file.

### D2.1 Network labels in `business_name` are found from the data, not from a guessed word list
- **Method:** a real business name belongs to one organization. A name shared by many **different TINs** is a contract or network label.
- **Evidence (distinct TINs per name):**

  | Name | TINs |
  |---|---|
  | "HEALTH NET DIRECT FFS COMMERCIAL" | 277 |
  | "PPO: Los Angeles County" | 38 |
  | "DIRECT NETWORK: AMBETTER PPO" | 16 |
  | "DIRECT NETWORK: AMBETTER HMO" | 15 |
  | "PPO: San Bernardino County" | 10 |
  | "PPO: Santa Clara County" / "PPO: San Diego County" | 6 each |

  Surnames like "LEE, M.D." (11 TINs) are name collisions between different doctors, not labels.
- **Alternatives:** a fixed keyword list. The first version missed "HEALTH NET DIRECT FFS COMMERCIAL", the largest label.
- **Limitation:** single-TIN labels are only caught by the keyword rules (IPA, "HealthNet PPO"). Medical groups like Regal and Lakeside (known CA HMO delegated groups) are *not* labelled yet. This is a hypothesis to test in C2.

## C2 Layer 1 — Plan labels from names + research
Script `src/stage2_plan_labels.py` → `data/stage2/plan_labels.parquet` (+ `.csv`).

### Evidence from the names (1,312 in-scope rows, 477 distinct names)
- Names follow `CA L|T|L&T [CUS] [CBA] <code> <network word> <cost-sharing>` (e.g. "CA L SC HMO 40/500d/4500"), plus product-style names ("Full Network HMO Gold $30", "Gold PPO 500/20", "CalChoice WholeCare HMO Gold A").
- Each family code goes with exactly one network word: SC → HMO (34/34 names), SAL → HMO (45/46, plus 1 with no word), CN → HMO (7/7). **EC goes with both HMO (24) and EOA (19)**, so EC is a network family and EOA is a plan type at the same level as HMO/PPO.

### Research (retrieved 2026-10-17)
- **EOA = Elect Open Access.** Health Net: "Elect Open Access (EOA) is an HMO plan…"; "Choose between two tiers of benefits – HMO or PPO" ([type of plans](https://www.healthnet.com/en_us/find-a-plan/large-employers/type-of-plans.html)). Official EOA flyer: members "Choose a participating physician group (PPG)… Then choose a primary care physician (PCP)"; "Through the PPO option, you have the freedom to see specialists in the PPO Network **for exams and evaluations** without referrals" ([EOA flyer](https://www.healthnet.com/content/dam/centene/healthnet/pdfs/broker/ca/general/EOA_flyer_eng.pdf)).
- **Tailored HMO networks:** "SmartCare, ExcelCare, and Salud HMO y Más" ([type of plans](https://www.healthnet.com/en_us/find-a-plan/large-employers/type-of-plans.html)). "EOA products are also found in our ExcelCare Network". This matches EC appearing with both HMO and EOA → **EC = ExcelCare** (an inference: the code itself isn't defined publicly).
- **CN = CanopyCare HMO** (Bay Area, launched 2021): broker source ([Word & Brown](https://wordandbrown.com/NewsPost/Large-Group-Carrier-Spotlight-Health-Net)). It fits the data: CN plans belong to Bay Area employers (Aon NCA, SFHSS, Children's Hospital Oakland).
- Health Net's large-employer plan-type page **does not list EPO or POS** as standard products. They appear only in custom (`CUS`) employer plans.
- **"PPO … - EPO" (index data):** the same plan_id 946002491 (Sacramento City USD) files "CUS HMO", "CUS PPO" and "CUS PPO … - EPO" side by side. Ironworkers (956042868) files EOA, POS, "PPO … EPO$15" and "PPO … EPO$10", with no plain PPO. *(A SCUSD renewal PDF seen in a search summary was cited earlier; on direct download it returns HTTP 404, so it is NOT used as evidence.)*

### D2.2 Priority order of rules (first match wins)
R1 EOA → R2 POS → R3 EPO → R4 HMO → R5 PPO → R6 HMO family code (SAL/SC/EC/CN/Salud/SmartCare/WholeCare) → R7 UNKNOWN.
- **Why this order:** the more restrictive or more specific word wins. A name with "PPO" and "EPO" is an EPO option. EOA is checked before HMO/PPO because an EOA has both tiers.
- **Alternatives:** majority vote of words; a learned classifier. Rejected: only 6 names are ambiguous, and explicit rules are easier to audit and explain.
- **Limitation:** it relies on Health Net naming conventions staying consistent.

### D2.3 EOA is NOT counted as PPO (label `EOA_HYBRID`)
- **Why:** Health Net itself calls EOA "an HMO plan". Members pick a PCP inside a physician group, as in an HMO, and the PPO tier only covers specialist exams/evaluations. Counting EOA as PPO would overstate the PPO network.
- **Alternatives:** PPO (it does use the PPO Network); separate "partial PPO".
- **Limitation:** EOA members **do** use PPO-network specialists, so PPO-network providers also serve EOA plans. Kept as its own label so the decision can be reversed in one line.

### D2.4 Two axes per plan: `product_type` (benefit rules) and `provider_network` (which doctors)
- **The problem:** 3 names carry both words: "CA L CUS PPO SACRAMENTO CITY USD- EPO", "CA L CUS PPO CA IRONWORKERS FWP EPO$15" and "…FWP-EPO$10". With a single label, either answer is a guess. (The first draft called them EPO; Member A rejected that as unproven.)
- **Evidence:** Health Net's own group EPO handbook: "Our EPO plan works like an HMO but gives you access to **our full PPO network** of doctors and hospitals… visit any doctor or hospital within Health Net's PPO network"; "Services received outside of the network… are only covered in the event of an emergency… This out-of-network coverage for non-emergent services is **the difference between an EPO plan and a PPO plan**" ([Health Net EPO handbook](https://www.healthnet.com/static/custom/unprotected/pdfs/CA58857_Northrop_handbook.pdf)). **Caveat, checked:** that PDF was created 2009-05-15 (form code "6019142 (5/09)"), is issued by Health Net Life Insurance Company, and covers one employer (Northrop Grumman). It shows how Health Net built a group EPO; it does **not** document these 3 plans. Index data: the same plan_id files separate plain PPO/HMO plans (SCUSD 946002491: "CUS HMO", "CUS PPO", "CUS PPO … - EPO").
- **Decision:** `product_type = EPO`: the name states EPO explicitly and a separate plain PPO exists for the same employer → ruled out as PPO **plans**. `provider_network = PPO_NETWORK_INFERRED`: likely the PPO doctors, but inferred, not documented. Confidence **medium**.
- **Applied to all labels:** PPO → PPO_NETWORK; EPO → PPO_NETWORK_INFERRED; HMO → HMO_NETWORK; EOA → HMO_NETWORK+PPO_SPECIALISTS; POS → UNCONFIRMED.
- **Alternatives:** (A) a separate "ambiguous" label, reported both ways; (B) count as PPO; (C) count as EPO. A–C all force one axis onto a question that has two.
- **Why:** the PS asks for both "the PPO network" (doctors) and "rule out EPO" (plan types). Two axes answer both without contradiction, and each axis is backed by a Health Net document.
- **Limitation:** no plan document for these 3 exact plans. The reading rests on Health Net's general group-EPO definition plus the sibling plans. Since every CA plan shares one rate file, the choice changes plan counts only, not providers or rates.
- **UPDATED (2026-10-18): the PPO-network inference is withdrawn.** A current California Department of Insurance filing
  ("Network Waivers for Health Net SBG PureCare One EPO") shows Health Net's EPO runs on its **own PureCare One EPO
  network**; it supersedes the 2009 handbook. The 3 plans are now `EPO_NETWORK` (not PPO, not on the PPO network) and the
  "456 incl. EPO" figure is dropped. See `docs/logic_ppo_network.md` §3.3.

### D2.5 No network word → HMO by family code, confidence medium
- "CA L SAL Mex 5/0/1500" (3 employers) and "CA L&T CUS SAL CEMENT MASONS CA H&W GF+": both are in the **SAL (Salud HMO y Más)** family, and every other SAL name says HMO.
- Glued token: "CA L CUSPPO 15/250/20% RAP" → PPO (other employers file "CA L CUS PPO 15/250/20% RAP").

### Result
| product_type | provider_network | Plan rows | Distinct names | Confidence |
|---|---|---|---|---|
| **PPO** | PPO_NETWORK | **453** | 133 | high |
| EPO | PPO_NETWORK_INFERRED | 3 | 3 | medium |
| HMO | HMO_NETWORK | 719 + 4 | 284 + 2 | high / medium (family) |
| EOA_HYBRID | HMO_NETWORK+PPO_SPECIALISTS | 127 | 49 | high |
| POS | UNCONFIRMED | 6 | 6 | high |
| UNKNOWN | — | 0 | 0 | — |

**PPO plans: 453 (documented on the PPO network). If the 3 EPO plans are added on the inferred network: 456.** Ruled out as PPO plans: 723 HMO, 127 EOA, 6 POS, 3 EPO.

**UPDATED (2026-10-18): this table is the *name-based* first pass. The final answer no longer depends on the name.**
`src/stage2_plan_design_evidence.py` matches each plan's benefit design to Health Net's 2026 plan documents (208 large-group
plan overviews + 242 SBCs: PPO documents = Certificate of Insurance + out-of-network benefits; HMO/EOA documents =
Evidence of Coverage, no out-of-network benefits; no design is sold both ways). Final network use: **PPO_NETWORK_PRIMARY
453 (393 proven by design / plan document, 60 name only)**, PPO_NETWORK_TIER 133 (EOA 127, POS 6), HMO_NETWORK_ONLY 723,
EPO_NETWORK 3. Where evidence exists (1,167 plans) it never contradicts the name. Per-plan evidence:
`data/stage2/ppo_network_decisions.csv`; logic: `docs/logic_ppo_network.md` §3.

The 453 PPO rows include 34 metal-tier names ("Gold PPO 500/20", HIOS IDs 67138CA0xx) and 2 rows with placeholder ID 999999999 (labelled by name; the ID is not used).

## C2 Step C — Can providers inside the file be split into PPO vs not? (evidence)
- **Label-group overlap:** of 3,734 NPIs, only **6** appear in more than one business-name label group (4 = DIRECT_FFS_COMMERCIAL + UNLABELLED, 2 = PPO_COUNTY + UNLABELLED); 3 of 2,888 TINs. Pairs per group: UNLABELLED 3,321, HEALTHNET_DIRECT_FFS_COMMERCIAL 297, PPO_COUNTY 100, AMBETTER_PPO 21, AMBETTER_HMO 19, IPA 14, DIRECT_NETWORK 6, HEALTHNET_PPO 2. All 74 capitation pairs are UNLABELLED. → The groups **partition** the providers. A Jaccard/containment matrix between them is ≈ 0 off the diagonal, so clustering them carries no information. Recorded as a finding; no heatmap of these groups is produced.
- **Outside ground truth searched:** Health Net's FHIR Provider Directory sits behind a login on Centene's developer portal (partners.centene.com), per [apis.io](https://apis.io/providers/centene/) and [AccessMediCal](https://github.com/jcl347/AccessMediCal). The public per-county Health Net directory JSON mentioned there is the **"LA Medi-Cal directory"** (Medicaid), so it doesn't apply to commercial PPO. No anonymous, machine-readable commercial PPO directory was found.
- **Cross-file check (added with C3):** the Ambetter CA in-network file is the only Centene CA file that fills `network_name` (100% of 3,141 provider_references), but every value is the single label **"Exchange CA"**, so it can't separate PPO from HMO either. 1,091 of the 3,734 Health Net CA NPIs also appear in that marketplace file.
- Together with D2.0 (one price per provider per service; `network_name` empty in 2,825/2,825 groups), the PTD data **cannot** say which providers are PPO-only vs HMO-only.

### D2.7 (CONFIRMED by Member A, 2026-10-17) PPO network rates = the whole CA in-network file, with HMO-style hints flagged, not dropped
- **Rule:** every provider in `…healthnet-ca_in-network.json` is kept for C4. Columns `network_hint` (from business-name labels: AMBETTER_HMO, IPA, AMBETTER_PPO, PPO_COUNTY, HEALTHNET_PPO, DIRECT_FFS_COMMERCIAL, DIRECT_NETWORK) and `in_capitation_code` let any user exclude HMO-style providers.
- **Why:** (1) all 453 PPO plans name this file, and only this file, as their in-network rate file, so all PPO in-network rates are in it (the PPO network ⊆ the file); (2) the file holds one price per provider per service, so no PPO-specific price is lost or mixed up; (3) nothing in the data separates providers by network.
- **Size of the uncertainty:** HMO-style hints touch at most 19 (AMBETTER_HMO) + 14 (IPA) + 74 (capitation) = 107 of 3,780 pairs (2.8%). We report the rate-file counts with and without them.
- **Alternatives:** (a) drop hinted providers (rejected: the hint labels' meaning is undocumented); (b) manual check in Health Net's online provider search for a sample of providers (recommended as validation if a team member has time; needs a person in a browser).
- **Limitation:** the result is an upper bound on the PPO network. Some providers may be in-network only for HMO/EOA plans.
- **UPDATED (2026-10-18): provider-level evidence added** (`src/stage2_provider_network.py`, `docs/logic_ppo_network.md` §4.1).
  210 (NPI, TIN) pairs are PPO_CONFIRMED (practice county outside Health Net's 31-county HMO service area per the CA Office
  of the Patient Advocate, or a PPO group label); 104 HMO_SIGNAL (capitation / IPA / Ambetter-HMO label); 3 MIXED;
  3,463 PPO_PRESUMED, priced like the confirmed PPO providers (median per-code difference 0.000). Every rate-file row
  carries `ppo_network_evidence`.

## C4 — Rate extraction
Script `src/stage4_extract_rates.py` → `data/stage4/rates.parquet` (+ `.csv`) and `data/stage4/c4_report.txt`.

> **UPDATED (2026-10-18), C4 v2: supersedes D4.3, D4.5 and D4.6 below** (full method: `docs/logic_ppo_network.md` §5).
> (1) Every price of every code type is extracted → `rates_all.parquet` (3,734 of 3,734 NPIs); `rates.parquet` is the
> HiLabs subset. (2) Facility prices are decoded: an RC item price whose `additional_information` names a CPT/HCPCS code
> is the facility fee for that code (`revenue_code` kept), so HiLabs codes priced rise 134 → 203 (68.8% → 75.6% of weight),
> professional and facility. (3) Service key = npi, tin, code type, code, item name (blank for decoded facility prices),
> billing class, modifier, place of service, revenue code, arrangement. (4) Several contracts for one service → the
> **primary contract's** price (the provider_reference pricing the most services), not the median: a median of two
> contracts is a price nobody is paid. `rate_median_all`, min, max, `n_contracts` are kept. (5) Expiry judged against
> the file's `last_updated_on`. (6) The out-of-network allowed-amounts file is parsed into `oon_allowed.parquet`
> (never mixed into rates). It gives provider signal O and the in- vs out-of-network benchmark.
> Result: 8,820 rows, 2,995 NPIs, 2,356 TINs, 203 codes.

### D4.3 Keep only the HiLabs billing codes
- **Rule:** `billing_code` ∈ the 583 PROC_CD values of the "Spec-Proc Weights" sheet.
- **Why:** the PS says rates are needed for the HiLabs Specialty→Code mapping; the final file needs a specialty for each code.
- **Result:** 134 codes found; 1,576 of 1,717 items skipped (mostly RC facility codes).

### D4.4 Keep only dollar amounts; drop ≤ $0 and expired
- **Rule:** negotiated_type ∈ {negotiated, fee schedule, derived}. Percentage and per diem are counted, not mixed in. Drop rate ≤ 0 and expiration_date < run date.
- **Why:** a median only makes sense over the same unit (dollars per service).
- **Result in this file:** 0 prices dropped by these rules (no $0, no percentage/per diem, every expiry 9999-12-31). The rules stay in the code for other files and re-runs.

### D4.5 Service key includes the item name, place of service, modifier and additional_information
- **Key:** (npi, tin, billing_code_type, billing_code, item_name, billing_class, modifier, service_code, additional_information).
- **Why:** the audit showed one billing code can carry different services (RC 250 = different drugs) and that `additional_information` holds sub-codes (home-health HIPPS). Place of service (office "11" vs outpatient hospital "22") is priced differently by design.
- **Limitation:** more rows per provider-code. The final Excel (C6) may collapse place of service if C chooses; the rows keep enough detail to do that.

### D4.6 Several prices for one key → keep the MEDIAN, with min / max / count / spread
- **Evidence:** 656 of 7,520 rows had >1 price; 381 had >1 *distinct* price. max/min ratio: median 1.35, p90 3.18, max 8.43 (e.g. NPI 1013950807 / TIN 941156581, 99213 at $105.19 and $855.00). The extra prices come from the same (NPI, TIN) sitting in several provider groups.
- **Alternatives:** most recent (impossible: every expiry is 9999-12-31 and there are no effective dates); minimum (biases low); mean (pulled by outliers like $855).
- **Why median:** robust to one extreme value and still a real negotiated level when n = 2 (midpoint).
- **Kept for audit:** rate_min, rate_max, n_rates, n_distinct_rates, rate_spread_ratio. 42 rows have spread > 3 → for C3's outlier metric.

### Verification
- Sample file: hand-computed answers reproduced (median $110 from $100/$120; $0 capitation price dropped; invalid NPI kept as TIN-only).
- Real file: 3 random rows recomputed independently from the raw JSON → identical rate and n.
- 7,520 rows; 2,824 NPIs; 2,231 TINs; 2,865 (NPI,TIN) pairs; 4 TIN-only rows; 134 codes; 100% professional.
- D2.7 sensitivity: 97 rows carry an HMO-style hint (AMBETTER_HMO / IPA / capitation); without them 7,423 rows and 2,795 NPIs.

## HiLabs supplement: "Specialty to Billing Code Mapping" (PS section 4)
File `data/raw/Rate Benchmarking Individual Weights_Hackathon.xlsx` (config key `paths.hilabs_xlsx`). No formulas, no nulls, no duplicate rows.

| Sheet | Rows | Columns | Content |
|---|---|---|---|
| Taxonomy-Spec Xwalk | 213 | SPEC_TYPE, SPEC_CD, SPEC_DESCR, Taxonomy Code | NUCC taxonomy → HiLabs specialty. **All 213 rows are SPEC_TYPE "Individual"**; each taxonomy maps to exactly 1 of 48 specialties |
| Specialty Weights | 48 | Spec Code, Specialty, Specialty Weight | One weight per specialty (largest: Primary Care - Midlevel 4,094,821; Primary Care 3,932,892) |
| Spec-Proc Weights | 946 | SPEC_CD, SPEC_DESCRIPTION, PROC_CD, SPEC_PROC_RANK, SPEC_PROC_WEIGHT | Specialty → billing codes, ranked and weighted. 583 distinct codes (409 CPT, 173 HCPCS, 1 CPT cat II/III); 54 specialties; 1–60 codes each (median 12); 118 codes shared by more than one specialty |

- **Inconsistency:** 6 specialty codes are in Spec-Proc only: 045 Surgical Services (Outpatient and ASC), 046 Skilled Nursing Facilities, 052 Inpatient Psychiatry, 072 Substance Use Disorder Rehabilitation Facility, 080 Urgent Care, P076 Children's Residential Treatment Facility. All are **facility** types and have **no taxonomy rows** in the crosswalk, so Type 2 (organization) NPIs cannot get a specialty through the crosswalk.

### Match against the CA in-network file (by billing_code)
- **134 of 583 HiLabs codes (23.0%) are priced**; weighted by SPEC_PROC_WEIGHT this is **68.8%** of HiLabs' code weight. Matched types: CPT 101, HCPCS 33. The file's 209 RC (revenue) codes can't collide (HiLabs codes are 5 characters).
- **All 2,720 prices on matched codes are `professional`.** None of the file's institutional (facility) prices fall on a HiLabs code.
- 2,865 of 3,775 (NPI, TIN) pairs (2,825 NPIs) are priced on at least one HiLabs code.
- By specialty (weight share priced): Emergency Medicine 99.2%, Primary Care - Midlevel 96.4%, Primary Care 91.8%, Pulmonology 89.6%, Cardiology 85.2% … Neurosurgery 9.7%, Cardiothoracic Surgery 4.7%, Dental - General (Pediatric) 0%.
- **Implications:** (C4) filtering to HiLabs codes keeps only professional prices; the 99.7% institutional share of the file falls outside HiLabs' list. (C3) code coverage = 23.0% by count / 68.8% by weight is a ready metric. (C6) specialty for Type 2 NPIs needs a source other than this crosswalk.

## Audit 2026-10-17: what is verified vs inferred (checked against the PS and Member B's review)
**V** = verified from data or an official source read directly. **I** = inferred (basis given). **X** = was wrong, now fixed.

| # | Claim | Status | Basis |
|---|---|---|---|
| 1 | All 11 index files downloaded completely | V | local bytes = server Content-Length for 11/11 |
| 2 | 3,275 plan rows; 1,312 CA group in scope | V | independent `json.load` count = parquet count |
| 3 | Ambetter excluded: individual market | V | `plan_market_type = individual` on all 1,120 rows (58 are CA by HIOS) |
| 4 | Fidelis = NY, QualChoice = AR | V | HIOS state chars: Fidelis NY 88 (+6 with non-state "EP"), QualChoice AR 85 |
| 5 | Health Net structure 1 = Oregon | V | file token `-or_`, HIOS state OR, no CA signal |
| 6 | Only 1 CA in-network file (no parts) | V | index: structure 0 lists 1 in-network file |
| 7 | Whole rate file parsed | V | 1,717 items read; 701,615,514 bytes = server; proper closing `}` |
| 8 | No remote provider lists | V | 0 of 2,825 provider_references have `location` |
| 9 | ~17% of NPIs are individuals (B: 665) | V | CMS NPI Registry API, random 120 NPIs: 20 Type 1 (16.7%) → ~622; 117/120 practise in CA |
| 10 | File ≈ one price list per provider | V | 0.49% within item; ≤2.64% even merging items by code |
| 11 | EOA is an HMO-based hybrid | V | Health Net page (fetched today, verbatim): "Elect Open Access (EOA) is an HMO plan…"; flyer (5/20) |
| 12 | Health Net large-employer page lists no EPO/POS | V | page text: 0 matches for EPO, POS, "Exclusive Provider", "Point of Service" |
| 13 | EC = ExcelCare, CN = CanopyCare, SC = SmartCare, SAL = Salud | I | name co-occurrence + Health Net page + broker source. **No label depends on EC/CN/SC** (all those names carry HMO/EOA words). SAL decides 4 rows, backed by 163 other SAL rows all saying HMO |
| 14 | 3 "PPO … EPO" plans: network = PPO network | **X** | 2009 handbook superseded by a current CA Dept. of Insurance filing: EPO = separate PureCare One EPO network → `EPO_NETWORK` (2026-10-18) |
| 15 | SCUSD renewal lists HMO/EPO/PPO | **X** | cited from a search summary; direct download = HTTP 404 → removed |
| 16 | Business-name labels ("PPO: <County>", "HEALTH NET DIRECT FFS COMMERCIAL", "DIRECT NETWORK: AMBETTER HMO/PPO", IPAs) mark network membership | I | They exist and repeat across many TINs (V). **What they mean is not documented.** Used only as one of four provider-level signals (with geography, capitation, price signature), never alone (2026-10-18) |
| 17 | CA scope proven by third-party data, not names | V | per structure: NPPES practice state of providers (CA 96.6%), DOL Form 5500 employer state (CA 76.3%), HIOS issuer = Health Net of California (CMS); Oregon structure 97.3% / 99.2% OR (`data/stage1/c1_scope_evidence.txt`, 2026-10-18) |
| 18 | Plan PPO / not-PPO decided from plan design + Health Net 2026 plan documents | V | 450 documents read; 0 designs shared PPO vs HMO/EOA; 1,167 plans classified without the network word, 0 disagreements (`data/stage2/c2_plan_design.txt`, 2026-10-18) |

### Open question the PS still requires us to answer (C4: "For the PPO network, extract rates for every provider")
Every CA plan, PPO or not, points to the same file. So **which providers in the file belong to the PPO network is not yet established**. **UPDATED (2026-10-18): answered with provider-level evidence, see the D2.7 update above and `docs/logic_ppo_network.md` §4.1.** The plan labels (C2 Layer 1) answer "which plans" and "which file". They do not answer "which providers". Step C (overlap of label groups) and an external check (e.g. Health Net's provider directory) are the routes to answer it. Until then, no provider is filtered out.

## Findings that reshape C2 (to design next)
- **Only ONE CA in-network file, shared by every Health Net CA plan.** A naive keyword count on plan names (first of PPO/HMO/EPO/POS/EOA found) gives HMO 719, PPO 455, EOA 127, POS 6, none 5. **This is NOT the C2 answer.** It ignores mixed names (e.g. "…PPO … EPO$15"), the unknown meaning of EOA, unlabeled plans, and the fact that a plan *named* PPO does not prove which provider network it uses. It's only the starting point C2 must test. So PPO can't be identified by choosing files; it has to be done inside the file. The planned file-level voting and file-overlap heatmap can't separate networks for CA as designed.
- First ~30 KB of the CA in-network file: `provider_references` comes before `in_network`; every `network_name` seen is `[]` (59 of 59); TINs are dash-formatted (`"68-0273974"`); NPIs are integers. One `business_name` reads **"PPO: Sacramento County"**, so network labels may leak into business names. This needs a full-file scan.
