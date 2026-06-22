# Solr Noise Cleanup Log

Tracks all noise term deletions from the `umls_core` Solr index, including what was removed, why, examples, and counts.

---

## Index Baseline

| Checkpoint | Doc Count |
|---|---|
| Original index | 4,271,299 |
| After `@` term removal | 4,078,582 |
| After batch noise removal (session 2) | 3,841,399 |

---

## Removal 1 — Terms containing `@`

**Date:** 2026-06-22
**Docs removed:** 192,717
**Index after:** 4,078,582

**Why removed:**
Terms with `@` in the `term_lower` field are unreachable by normal prefix-based autocomplete. A clinician typing in a note will never type `@` as part of a medical term search. These terms exist due to internal notation conventions in their source vocabularies.

**Breakdown by source:**

| Source | Count |
|---|---|
| ICD10PCS | 192,543 |
| OMIM | 45 |
| CPT | 38 |
| LNC | 36 |
| MEDCIN | 32 |
| MTH | 15 |
| MSH | 8 |

**What `@` means per source:**
- **ICD10PCS** — hierarchy path delimiter: `Administration @ Circulatory @ Transfusion @ Bone Marrow`
- **OMIM** — gene/chemical prefix marker: `21-@HYDROXYLASE POLYMORPHISM`, `5-@FLUOROURACIL TOXICITY`
- **CPT/MEDCIN/MTH** — official HGNC gene locus symbol: `IGH@ gene rearrangement analysis by PCR`
- **LNC** — "at" in natural language: `Body height @ birth Measured`, `Age @ 1st pregnancy`
- **MSH** — radiotracer/chemical shorthand: `(18F)FE@IPCIT`, `FE@SUPPY`

**Examples:**
```
Administration @ Circulatory                                [ICD10PCS]
Administration @ Circulatory @ Transfusion @ Bone Marrow @ Percutaneous @ Fresh Plasma  [ICD10PCS]
21-@HYDROXYLASE POLYMORPHISM                               [OMIM]
2,4-@DIENOYL-CoA REDUCTASE DEFICIENCY                     [OMIM]
IGH@ gene rearrangement analysis by PCR                    [CPT/MEDCIN]
Body height @ birth Measured                               [LNC]
Age @ 1st pregnancy                                        [LNC]
(18F)FE@IPCIT                                              [MSH]
IGHV@ gene cluster                                         [MTH]
```

**Solr delete query:**
```json
{"delete": {"query": "term_lower:*@*"}}
```

**populate_terms.py fix:** Added `if '@' in term_lower: continue` in the row processing loop so these are excluded on future re-indexes.

---

## Removal 2 — Batch noise cleanup

**Date:** 2026-06-22
**Docs removed:** 237,183
**Index after:** 3,841,399

### 2a — HTML entity terms (`&#`)

**Docs removed:** ~56,000 (LPDN tty + LG tty overlap)

**Why removed:**
LOINC LPDN (Long Common Name with Panel Display Name) terms stored raw HTML-encoded pipe characters (`&#x7C;` = `|`). These are broken display strings that would render as literal `&#x7C;` in any UI. No clinician would ever type this.

**TTYs affected:** LPDN, LG

**Examples:**
```
2-methyl AP-237 &#x7C; Urine &#x7C; Drug toxicology                          [LNC LPDN]
21-Deoxycortisol &#x7C; Serum or Plasma &#x7C; Chemistry - non-challenge      [LNC LPDN]
2-spotted spider mite (Tetranychus urticae) IgE &#x7C; Serum &#x7C; Allergy   [LNC LPDN]
21-Deoxycortisol&#x7C;Pt&#x7C;Bld.dot                                         [LNC LG]
Cells.CD3+CD4+&#x7C;Bld                                                        [MTH PN]
```

**Solr delete queries:**
```json
{"delete": {"query": "term_lower:*\\u0026\\u0023*"}}
{"delete": {"query": "tty:LG"}}
```

---

### 2b — MTH_LN (LOINC full axis panel paths)

**Docs removed:** 102,435

**Why removed:**
LOINC MTH_LN terms are the full formal axis notation used internally by LOINC — colon-delimited strings encoding the component, property, timing, system, scale, and method axes. These are machine-readable identifiers, not human-readable terms. No clinician types in this format.

**TTY:** MTH_LN

**Examples:**
```
2-methyl AP-237:Presence or Threshold:To identify measures at a point in time:Urine:Ordinal:Confirm
21-Deoxycortisol:Mass Concentration:To identify measures at a point in time:Dried blood spot:Quantitative
21-Deoxycorticosterone^1 hour post dose corticotropin:Substance Concentration:To identify measures at a point in time:Serum/Plasma:Quantitative
2-minute Walk Endurance Test - scale score^^adjusted for age+sex+race+ethnicity+educational attainment:Score:To identify measures at a point in time:^Patient:Quantitative:NIH Toolbox
```

**Solr delete query:**
```json
{"delete": {"query": "tty:MTH_LN"}}
```

---

### 2c — CCN (clinical/chemical code numbers)

**Docs removed:** 16,444

**Why removed:**
CCN terms are internal NCI/PDQ identifier codes — alphanumeric strings like `2141 V-11`, `23ME-00610`, `256U87 Hydrochloride`. These are drug/compound identifiers used by registries, not terms a clinician would search for in an autocomplete box.

**TTY:** CCN
**Sources:** NCI, PDQ

**Examples:**
```
2141 V-11        [NCI]
2141 V11         [NCI]
23ME-00610       [NCI]
23ME-01473       [NCI]
256U87 Hydrochloride  [NCI/PDQ]
27-400           [PDQ]
27T51            [NCI]
242 DL           [NCI]
```

**Solr delete query:**
```json
{"delete": {"query": "tty:CCN"}}
```

---

### 2d — `=` sign terms (LNC answer list labels)

**Docs removed:** ~2,168 (LA tty + residual across other TTYs)

**Why removed:**
LOINC answer list labels that encode questionnaire scale values using `=` notation. These are internal scoring labels used by LOINC panels (PHQ-9, pain scales, etc.), not clinical terms. A clinician would type `moderate` not `3 = Moderate`.

**TTYs affected:** LA (primary), residual in FN, PN

**Examples:**
```
3 = Moderate                [LNC LA]
0 = None                    [LNC LA]
7 = Very severe             [LNC LA]
20-27 = Severe depression   [LNC LA]
1 = 1 day                   [LNC LA]
3 = Between a month and a year  [LNC LA]
6-10 mitoses per 10 high power fields (score = 2)  [SNOMEDCT_US FN]
```

**Solr delete queries:**
```json
{"delete": {"query": "term_lower:*\\u003D*"}}
{"delete": {"query": "tty:LA"}}
```

---

### 2e — LPDN (pipe-separated LOINC display names)

**Docs removed:** 95,724

**Why removed:**
LOINC LPDN (Long Panel Display Name) terms use pipe-separated format to concatenate the analyte, specimen, and panel category. Even without HTML encoding, these are composite display strings not typed as search queries. Many also contain `^^` double-caret notation and `XXX` placeholders.

**TTY:** LPDN

**Examples:**
```
2-methyl AP-237/Creatinine                                                   [LNC]
2-minute Walk Endurance Test - scale score^^adjusted for age                 [LNC]
2-minute Walk Endurance Test - scale score^^adjusted for age+sex+race+ethnicity+educational attainment  [LNC]
2.17 hours post XXX challenge                                                [LNC]
2.25 hours post XXX challenge                                                [LNC]
2-spotted spider mite (Tetranychus urticae) IgE                              [LNC]
```

**Solr delete query:**
```json
{"delete": {"query": "tty:LPDN"}}
```

---

## Removal 3 — Terms with no alphabetic characters (pure numbers/symbols)

**Date:** 2026-06-22
**Docs removed:** 1,361
**Index after:** 3,840,038

**Why removed:**
Terms whose stored value contains zero alphabetic characters (a-z, A-Z). These are pure number strings, bare symbols, or numeric code notation — none of which a clinician would ever type into an OPD autocomplete box. Verified by streaming the full index and checking each stored term value in Python (Solr's tokenized field prevents accurate regex matching server-side).

**Breakdown by semantic type:**

| Semantic Type | Count | Examples |
|---|---|---|
| Spatial Concept | 962 | `3: 10181630-10255723`, `3: 38055700-38139233` — NCI genomic coordinates |
| Quantitative Concept | 173 | `30`, `300`, `3000`, `3.2`, `3.3` — bare numbers |
| Classification | 60 | `311`, `314`, `315` — SNOMEDCT bare numeric classification codes |
| Finding | 52 | `3/60`, `3/12`, `3/4` — visual acuity fractions |
| Organic Chemical | 36 | `32-1328` — chemical registry codes |
| Pharmacologic Substance | 19 | numeric drug identifier codes |
| Intellectual Product | 16 | bare numeric codes |
| Other | 43 | `%`, `+`, `<`, `=`, `>`, `3.1.1.34` (enzyme EC numbers) |

**Sources:** NCI (1,046), SNOMEDCT_US (140), MTH (98), MSH (58), CHV (16), MMSL (2), OMIM (1)

**Method:** Streamed all docs with `term_length:[1 TO 50]`, filtered in Python for `not any(c.isalpha() for c in term)`, collected 1,361 IDs, deleted by ID batch (100 IDs per Solr update request). Terms with length > 50 were verified to always contain alphabetic characters.

---

## Removal 4 — MSH research/taxonomy noise (25 semantic types)

**Date:** 2026-06-22
**Docs removed:** 709,779
**Index after:** 3,130,259

**Why removed:**
MSH (MeSH) indexes the full biomedical literature vocabulary including pure research biology, biochemistry, and taxonomy — the vast majority of which has no OPD clinical use. Sampled 50 terms per type; zero recognizable OPD terms found in any removed type. All real clinical drug names present in MSH also exist in RXNORM, SNOMEDCT_US, MMSL, or NCI at higher source priority — verified for Penicillin, Aspirin, Amoxicillin, Metformin, Paracetamol, Ibuprofen.

**Delete query file:** `data/msh_noise_deletes.txt`

| Semantic Type | Docs Removed | Reason |
|---|---|---|
| Organic Chemical | 213,172 | IUPAC strings, chemical compound codes |
| Amino Acid, Peptide, or Protein | 167,749 | Species-tagged research proteins (mouse/rat/Xenopus) |
| Biologically Active Substance | 82,591 | Species-tagged research proteins |
| Pharmacologic Substance | 74,661 | Research compound codes (YM553, B-10610, IUPAC drug strings) |
| Fungus | 61,383 | Mycology taxonomy |
| Enzyme | 34,244 | Species-tagged biochemistry enzymes |
| Plant | 3,983 | Lichen/plant taxonomy |
| Nucleic Acid, Nucleoside, or Nucleotide | 16,172 | Molecular biology — microRNA, modified nucleosides |
| Bacterium | 15,507 | Bacterial taxonomy (Streptomyces spp., etc.) |
| Immunologic Factor | 8,547 | Species-tagged antigens and research proteins |
| Receptor | 8,308 | Species-tagged receptor proteins |
| Indicator, Reagent, or Diagnostic Aid | 6,731 | Lab reagents (DNA-Sephadex, TRITC-RCA I) |
| Inorganic Chemical | 3,928 | Inorganic chemistry compounds |
| Hazardous or Poisonous Substance | 3,603 | Toxicology research compounds |
| Antibiotic | 2,850 | Obscure antibiotic codes and IUPAC strings |
| Mammal | 1,541 | Zoology taxonomy |
| Hormone | 1,403 | Species-tagged hormones, insect growth regulators |
| Eukaryote | 1,378 | Insect/arachnid taxonomy |
| Reptile | 630 | Reptile taxonomy |
| Gene or Genome | 624 | Gene entries |
| Fish | 264 | Fish taxonomy |
| Amino Acid Sequence | 194 | Structural biology sequence concepts |
| Nucleotide Sequence | 123 | Molecular biology sequence notation |
| Amphibian | 111 | Amphibian taxonomy |
| Bird | 82 | Bird taxonomy |

**MSH before:** 784,956 → **MSH after:** 75,177

---

## Removal 5 — FEF spirometry terms (`term_lower:fef*`)

**Date:** 2026-06-22
**Docs removed:** 92
**Index after:** 3,130,161

**Why removed:**
FEF (Forced Expiratory Flow) terms are spirometry measurement values recorded by pulmonologists during PFT reports — not terms a doctor types in an OPD autocomplete box. All variants (FEF25%, FEF25-75%, FEF50%, FEF75%) come from MEDCIN structured pulmonology documentation. None represent OPD chief complaints or diagnoses.

**Breakdown by source:**

| Source | Count |
|---|---|
| MEDCIN | 74 |
| LNC | 9 |
| NCI | 4 |
| CHV | 2 |
| SNOMEDCT_US | 2 |
| MSH | 1 |

**Examples:**
```
FEF25-75%                                             [MEDCIN PT]
FEF25-75% change after bronchodilator as % of predicted value  [MEDCIN PT]
FEF25-75% post-bronchodilation Z score                [MEDCIN PT]
FEF50% percentile pre-bronchodilation                 [MEDCIN PT]
spirometry FEF 25-75% predicted value                 [MEDCIN SY]
FEF 25-75 - Forced expiratory flow rate between 25 and 75% of vital capacity  [SNOMEDCT_US SY]
FEF 25-75% --post bronchodilation                     [LNC LC]
```

**Solr delete query:**
```json
{"delete":{"query":"term_lower:fef*"}}
```

---

## Removal 6 — Organism taxonomy (9 semantic types, all sources)

**Date:** 2026-06-22
**Docs removed:** 73,971
**Index after:** 3,056,190

**Why removed:**
Pure biological taxonomy — species names, genera, families of bacteria, fish, birds, insects, fungi, plants, mammals, reptiles, and amphibians. No OPD doctor types `Streptomyces rutgersensis subspecies castelarensis` or `Roughspine sculpin` into a clinical autocomplete. Note: MSH entries for these types were already removed in Removal 4; this sweep covers SNOMEDCT_US, NCI, CHV, MTH, and other sources. Parasite organism names (e.g. `Trichinella spiralis larva`) are removed here — the corresponding diagnoses (`Trichinellosis`) live in Finding/Disease semantic types and are preserved.

**Breakdown:**

| Semantic Type | SNOMED | NCI | Other | Total |
|---|---|---|---|---|
| Bacterium | 25,463 | 1,203 | 1,580 | 28,246 |
| Eukaryote | 7,461 | 201 | 3,490 | 11,152 |
| Fungus | 5,619 | 364 | 1,633 | 7,616 |
| Bird | 4,944 | 20 | 719 | 5,683 |
| Fish | 4,871 | 47 | 627 | 5,545 |
| Plant | 3,395 | 1,478 | 4,222 | 9,095 |
| Mammal | 2,929 | 932 | 949 | 4,810 |
| Reptile | 1,241 | 0 | 186 | 1,427 |
| Amphibian | 220 | 9 | 168 | 397 |
| **Total** | | | | **73,971** |

**Examples removed:**
```
Salmonella enterica subsp. enterica ser. Marshall     [SNOMEDCT_US Bacterium]
Brevibacterium yomogidense                            [SNOMEDCT_US Bacterium]
Roughspine sculpin                                    [SNOMEDCT_US Fish]
Blue-headed pionus                                    [SNOMEDCT_US Bird]
Aedes scapularis                                      [SNOMEDCT_US Eukaryote]
Aspergillus violaceofuscus                            [SNOMEDCT_US Fungus]
C3Smn.CB17-Prkdc-scid/J Mouse                        [NCI Mammal]
Crotalus vegrandis                                    [SNOMEDCT_US Reptile]
Tennessee cave salamander                             [SNOMEDCT_US Amphibian]
Velvet grass pollen                                   [SNOMEDCT_US Plant]
```

**Solr delete queries:**
```json
{"delete":{"query":"semantic_type:\"Bacterium\""}}
{"delete":{"query":"semantic_type:\"Fish\""}}
{"delete":{"query":"semantic_type:\"Bird\""}}
{"delete":{"query":"semantic_type:\"Eukaryote\""}}
{"delete":{"query":"semantic_type:\"Fungus\""}}
{"delete":{"query":"semantic_type:\"Plant\""}}
{"delete":{"query":"semantic_type:\"Mammal\""}}
{"delete":{"query":"semantic_type:\"Reptile\""}}
{"delete":{"query":"semantic_type:\"Amphibian\""}}
```

---

## Summary Table

| Removal | Pattern / TTY | Docs Removed | Date |
|---|---|---|---|
| 1 | `@` in term_lower | 192,717 | 2026-06-22 |
| 2a | HTML entities (`&#`) — LPDN + LG | ~56,000 | 2026-06-22 |
| 2b | MTH_LN tty | 102,435 | 2026-06-22 |
| 2c | CCN tty | 16,444 | 2026-06-22 |
| 2d | `=` sign terms — LA tty | ~2,168 | 2026-06-22 |
| 2e | LPDN tty | 95,724 | 2026-06-22 |
| 3 | Pure number/symbol terms (no alpha chars) | 1,361 | 2026-06-22 |
| 4 | MSH research/taxonomy — 25 semantic types | 709,779 | 2026-06-22 |
| 5 | FEF spirometry terms (`term_lower:fef*`) | 92 | 2026-06-22 |
| 6 | Taxonomy — 9 organism semantic types (all sources) | 73,971 | 2026-06-22 |
| 7 | Biochemistry/genomics — 12 semantic types (all sources) | 328,144 | 2026-06-22 |
| 8 | Language semantic type | 2,728 | 2026-06-22 |
| 9 | Geographic Area semantic type | 10,997 | 2026-06-22 |
| 10 | Research Activity semantic type | 2,692 | 2026-06-22 |
| 11 | Molecular Biology Research Technique semantic type | 1,132 | 2026-06-22 |
| 12 | Professional Society semantic type | 104 | 2026-06-22 |
| 13 | Machine Activity semantic type | 309 | 2026-06-22 |
| 14 | Research Device semantic type | 214 | 2026-06-22 |
| 15 | Organization semantic type | 676 | 2026-06-22 |
| **Total** | | **~1,597,687** | |

---

## Index Checkpoints

| Checkpoint | Doc Count |
|---|---|
| Original index | 4,271,299 |
| After `@` term removal | 4,078,582 |
| After batch noise removal (session 2) | 3,841,399 |
| After pure number/symbol removal | 3,840,038 |
| After MSH research/taxonomy removal | 3,130,259 |
| After FEF spirometry removal | 3,130,161 |
| After organism taxonomy removal (9 types) | 3,056,190 |
| After biochemistry/genomics removal (12 types) | 2,728,046 |
| After Language removal | 2,725,318 |
| After Geographic Area removal | 2,714,321 |
| After Research Activity removal | 2,711,629 |
| After Molecular Biology Research Technique removal | 2,710,497 |
| After Professional Society + Machine Activity + Research Device + Organization removal | 2,709,194 |

---

## Removal 7 — Biochemistry/genomics noise (12 semantic types, all sources)

**Date:** 2026-06-22
**Docs removed:** 328,144
**Index after:** 2,728,046

**Why removed:**
Pure research biochemistry and genomics — gene entries, molecular proteins, enzymes, receptors, nucleic acid sequences, and related molecular biology concepts. No OPD doctor types `RBFOX1 wt Allele`, `Alpha-1,6-mannosyl-glycoprotein beta-1,2-N-acetylglucosaminyltransferase`, or `PHD Finger Motif` into a clinical autocomplete. Organic Chemical was intentionally excluded from this batch — it contains real drug names mixed with IUPAC noise and needs source-level handling.

**Types removed:**

| Semantic Type | Docs Removed | Representative examples |
|---|---|---|
| Gene or Genome | 205,181 | `RBFOX1 wt Allele`, `PLAC9P1 gene`, `KCNA3 gene` |
| Amino Acid, Peptide, or Protein | 55,361 | `PR Domain-Containing Protein 4`, `Cyclin-D2` |
| Immunologic Factor | 24,121 | `HLA Class II Histocompatibility Antigen DQ Beta 1 Chain`, `CCL3` |
| Biologically Active Substance | 18,160 | `Pre-MicroRNA 30C2`, `MIRN4701 microRNA, human` |
| Enzyme | 12,816 | `Alpha-1,6-mannosyl-glycoprotein beta-1,2-N-acetylglucosaminyltransferase`, `DNA Polymerase II` |
| Nucleic Acid, Nucleoside, or Nucleotide | 4,839 | `Chromosome 17 Centromere Probe`, `NCRNA00072` |
| Receptor | 3,108 | `G Protein-Coupled Receptor 158`, `Interleukin-2 Receptor Beta Chain` |
| Inorganic Chemical | 3,033 | `Boron carbide`, `water O-15`, `Calcium Sulfate Hemihydrate` |
| Amino Acid Sequence | 379 | `KDEL Motif`, `SH3-Binding Motif`, `PHD Finger Motif` |
| Nucleotide Sequence | 368 | `Origin of Replication`, `TNFd3 Allele`, `Scaffold-Associated Region` |
| Archaeon | 565 | `Methanocaldococcus`, `Archaeoglobus`, `Haloarcula altuensis` |
| Experimental Model of Disease | 213 | `UACC-257`, `SW-620`, `NCI/ADR-RES` — cell lines and animal models |
| **Total** | **328,144** | |

**Solr delete queries:**
```json
{"delete":{"query":"semantic_type:\"Gene or Genome\""}}
{"delete":{"query":"semantic_type:\"Amino Acid, Peptide, or Protein\""}}
{"delete":{"query":"semantic_type:\"Immunologic Factor\""}}
{"delete":{"query":"semantic_type:\"Biologically Active Substance\""}}
{"delete":{"query":"semantic_type:\"Enzyme\""}}
{"delete":{"query":"semantic_type:\"Nucleic Acid, Nucleoside, or Nucleotide\""}}
{"delete":{"query":"semantic_type:\"Receptor\""}}
{"delete":{"query":"semantic_type:\"Inorganic Chemical\""}}
{"delete":{"query":"semantic_type:\"Amino Acid Sequence\""}}
{"delete":{"query":"semantic_type:\"Nucleotide Sequence\""}}
{"delete":{"query":"semantic_type:\"Archaeon\""}}
{"delete":{"query":"semantic_type:\"Experimental Model of Disease\""}}
```

---

## Removal 8 — Language semantic type

**Date:** 2026-06-22
**Docs removed:** 2,728
**Index after:** 2,725,318

**Why removed:**
Human language names — `Classical Arabic language`, `Nuer language`, `Panamanian sign language`, `Fon Language`, `Abkhazian Language`. No OPD clinical relevance whatsoever. Sources: NCI (1,588), SNOMEDCT_US (962), MTH (137), CHV (39), MSH (2).

**Solr delete query:**
```json
{"delete":{"query":"semantic_type:\"Language\""}}
```

---

## Removal 9 — Geographic Area semantic type

**Date:** 2026-06-22
**Docs removed:** 10,997
**Index after:** 2,714,321

**Why removed:**
Entirely US county names, country names, UK regions, Puerto Rico municipios, and accident location descriptors. Examples: `Jones County`, `Travis County Texas`, `Merseyside`, `Egypt`, `Poland`, `Place of occurrence of accident or poisoning, bank`. No OPD doctor types a county name into a clinical autocomplete. Sources: NCI (9,020), SNOMEDCT_US (857), CHV (564), MSH (461), MTH (92), LNC (3).

**Solr delete query:**
```json
{"delete":{"query":"semantic_type:\"Geographic Area\""}}
```

---

## Removal 10 — Research Activity semantic type

**Date:** 2026-06-22
**Docs removed:** 2,692
**Index after:** 2,711,629

**Why removed:**
Study design terms, clinical trial nomenclature, lab staining protocols, and research methodology — `Cross Validation`, `Double Blind Study`, `Randomization`, `Meta-Analysis`, `Patch-Clamp Technique`, `Van Gieson stain technique`, `Cohort Analysis`. No OPD doctor types these in a clinical autocomplete. Sources: NCI (1,334), MSH (638), CHV (485), SNOMEDCT_US (139), MTH (74), others (22).

**Solr delete query:**
```json
{"delete":{"query":"semantic_type:\"Research Activity\""}}
```

---

## Removal 11 — Molecular Biology Research Technique semantic type

**Date:** 2026-06-22
**Docs removed:** 1,132
**Index after:** 2,710,497

**Why removed:**
Pure genomics and molecular lab methods — `Next Generation Sequencing`, `Single Cell RNA Sequencing`, `CRISPR/Cas9 Gene Editing`, `Whole Genome Shotgun Sequencing`, `In Situ Hybridization`, `RNA-Seq`, `Droplet Digital PCR`. These are lab report and research paper terms, not OPD clinical terms. Sources: NCI (581), MSH (312), SNOMEDCT_US (82), CHV (77), PDQ (32), MTH (24), MEDCIN (12), MDR (11), CPT (1).

**Solr delete query:**
```json
{"delete":{"query":"semantic_type:\"Molecular Biology Research Technique\""}}
```

---

## Removal 12 — Professional Society semantic type

**Date:** 2026-06-22
**Docs removed:** 104
**Index after:** (part of batch below)

**Why removed:**
Medical/pharma professional society names — `American College of Radiology`, `American Academy of Periodontology`, `Societies, Pharmaceutical`, `Veterinary Societies`. No clinician types a society name in OPD autocomplete.

**Solr delete query:**
```json
{"delete":{"query":"semantic_type:\"Professional Society\""}}
```

---

## Removal 13 — Machine Activity semantic type

**Date:** 2026-06-22
**Docs removed:** 309
**Index after:** (part of batch below)

**Why removed:**
Computer/technology process terms — `Data Mining`, `Natural Language Processing`, `Computer Modeling`, `Virtual Reality`, `Automated Facial Identity Recognition`, `Fisher's Linear Discriminant Analysis`, `Singular Value Decomposition`. Pure informatics/tech concepts, zero OPD use.

**Solr delete query:**
```json
{"delete":{"query":"semantic_type:\"Machine Activity\""}}
```

---

## Removal 14 — Research Device semantic type

**Date:** 2026-06-22
**Docs removed:** 214
**Index after:** (part of batch below)

**Why removed:**
Laboratory research equipment — `DNA microarrays`, `Bacterial Artificial Chromosome`, `Tissue Chip`, `Yeast Artificial Chromosome`, `Organ-on-a-Chip Devices`, `Immunomagnetic Selection Column`. Research instrumentation with no OPD use.

**Solr delete query:**
```json
{"delete":{"query":"semantic_type:\"Research Device\""}}
```

---

## Removal 15 — Organization semantic type

**Date:** 2026-06-22
**Docs removed:** 676
**Index after:** 2,709,194

**Why removed:**
Government agencies, commercial organizations, research institutes — `United States Office of National Drug Control Policy`, `Historically Black Colleges and Universities`, `UNHCR`, `Editas Medicine`, `ORCID`, `Food processing industry`. No OPD clinical relevance.

**Solr delete query:**
```json
{"delete":{"query":"semantic_type:\"Organization\""}}
```

---

## Pending (under review)

| Pattern | Count | Notes |
|---|---|---|
| OSN — LOINC short codes | 87,327 | Mix of cryptic abbreviations and some readable terms — needs sampling |
| ETAL — OMIM allele names ALL CAPS | 33,549 | Duplicates of proper-case terms — likely safe to remove |
| PN + NOCODE — MTH internal constructs | 153,663 | Already lowest ranked (tty_priority=2) — decision pending |
