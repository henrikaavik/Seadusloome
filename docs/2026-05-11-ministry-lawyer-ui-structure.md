# Seadusloome UI Structure for Ministry Lawyers

Date: 2026-05-11

Scope: Product and UI structure for Seadusloome as a legal advisory workbench for lawyers and officials in Estonian ministries.

Reference context: Section 7 of `eesti-oigusontoloogia-ulevaade.pdf` describes ministry use cases: norm impact chains, EU coordination, KOV policy comparison, competence audits, sanctions comparison, legislative burden management, crisis/internal-security legal mapping, and public-service full views.

## Executive Direction

Seadusloome should be structured around legal work, not around technical modules.

The current application exposes the internal system shape:

- `Uurija`
- `Eelnõud`
- `Koostaja`
- `Vestlus`

That is understandable to a development team, but it makes ministry lawyers translate their task into a tool choice. A lawyer does not start with "I need a graph explorer" or "I need a RAG chat". They start with:

- "I need to know what this amendment affects."
- "I need to check whether an EU directive is fully transposed."
- "I need to compare KOV regulations."
- "I need to know which agency is responsible for supervision."
- "I need to see whether this sanction level is consistent."
- "I need advice on how to fix the draft."

The product should therefore present itself as a legal advisory workbench. The technical modules still exist underneath, but the user-facing structure should be organized by ministry workflows.

The central design rule:

> The system offers legal analysis, options, and recommended next actions. It should not ask lawyers technical questions.

This means the UI must not ask about SPARQL, RDF classes, named graphs, graph URIs, embedding search, ontology predicates, background jobs, or other implementation details. When the system needs more information, it should ask legal or policy questions in ordinary ministry language.

## Primary Users

The primary users are lawyers and officials who work in ministries and participate in law creation, legal analysis, coordination, review, and impact assessment.

They may be skilled lawyers, but they should not need to understand the ontology technology. The product should treat ontology, RAG, graph traversal, and SPARQL as invisible infrastructure.

The expected user goals are:

- analyze a legislative idea before drafting;
- upload and assess a draft law or VTK;
- understand legal dependencies and conflicts;
- identify EU, KOV, court-practice, competence, and sanction implications;
- get advice on legal solutions;
- create or improve draft text;
- prepare evidence for internal review or coordination.

## Product Principle

Seadusloome should behave like a senior legal analyst with access to the full legal ontology.

The system should:

- infer likely context from the user's draft, idea, ministry, and selected topic;
- run the relevant ontology analyses automatically;
- show findings in a legally meaningful order;
- recommend what to do next;
- ask clarifying questions only when legal judgment is genuinely needed;
- always expose evidence and source links;
- make it easy to export, annotate, assign, and continue drafting.

The system should not:

- ask the user to choose technical query types;
- expose raw ontology identifiers as primary UI;
- require users to know which module can answer a legal question;
- make the graph the only way to understand relationships;
- make the chat the main access point for structured analysis;
- leave users with findings but no proposed solution.

## Recommended Top-Level Navigation

Proposed primary navigation:

1. `Töölaud`
2. `Analüüsikeskus`
3. `Eelnõud`
4. `Õiguskaart`
5. `Koostaja`
6. `Nõustaja`
7. `Ülevaatus`
8. `Admin`

### Why This Structure

`Töölaud` is the user's daily queue.

It should answer:

- what needs my attention today;
- which drafts are waiting for review;
- which analyses changed after ontology updates;
- which findings are unresolved;
- what the system recommends next.

`Analüüsikeskus` is the main new structure.

It should be the home for ministry use cases from Section 7. Instead of asking the user to pick a technical feature, it offers legal analysis workflows.

`Eelnõud` remains the document workspace.

It should handle uploads, versions, VTK links, processing status, impact reports, exports, retention, and deletion.

`Õiguskaart` is the visual ontology map.

It should no longer be framed as the primary "Explorer". It should be a supporting evidence view that opens from analyses with context already applied.

`Koostaja` remains the drafting workflow.

It should use the analysis workflows as inputs and should propose legal structure, clauses, alternatives, and risk notes.

`Nõustaja` is a better label than `Vestlus`.

The user is not looking for "chat"; the user is asking for legal advice. The assistant should also appear inside reports and draft pages, not only as a separate page.

`Ülevaatus` is the collaboration and legal validation area.

It should collect unresolved findings, annotations, ministry confirmations, assigned legal checks, and review decisions.

`Admin` stays technical/administrative and should be hidden from normal lawyers.

## Core UI Pattern for Every Workflow

Every legal workflow should follow the same structure:

1. `Sisend`
2. `Ulatus`
3. `Tulemused`
4. `Tõendid`
5. `Soovitatud tegevused`

### 1. Sisend

The user gives a legal object or describes a legal issue:

- draft law;
- VTK;
- paragraph reference;
- CELEX number;
- institution;
- sanction type;
- public service;
- topic such as waste management, cybersecurity, or social benefits.

The UI should accept natural language and structured references.

Good input examples:

- "Muudame AvTS § 35."
- "Kontrolli AI määruse ülevõttu."
- "Võrdle jäätmehoolduseeskirju KOVide lõikes."
- "Millistel asutustel on selle valdkonna järelevalvepädevus?"

Bad input examples:

- "Select RDF class."
- "Enter graph URI."
- "Choose SPARQL traversal depth."
- "Pick embedding index."

### 2. Ulatus

The system should choose a sensible default scope and let the user adjust it in legal terms.

Scope controls should be legal:

- current law vs historical redactions;
- Estonia only vs Estonia + EU;
- all ministries vs my organization;
- national law vs national + KOV regulations;
- court practice included or excluded;
- draft lifecycle stage;
- time period.

The system can say:

> Analüüsin kehtivat õigust, seotud eelnõusid, Riigikohtu praktikat ja EL õigusakte. Võite ulatust muuta.

It should not ask:

> Which named graphs should be included?

### 3. Tulemused

The first result screen should be decision-oriented.

It should not start with a table of raw entities. It should start with:

- key findings;
- risk level;
- recommended next action;
- legal rationale;
- confidence;
- what changed compared with earlier analysis, if relevant.

Example:

> Kavandatav muudatus mõjutab vähemalt 18 sätet, 3 pooleliolevat eelnõu ja 2 Riigikohtu lahendit. Kõrgeim risk on vastuolu AvTS § 35 erandite süsteemiga. Soovitus: täpsustada andmekoosseisude avalikustamise erandit ja lisada üleminekusäte.

### 4. Tõendid

Every claim must be auditable.

The UI should show:

- source provision;
- linked draft;
- court decision;
- EU act;
- relation type in legal language;
- quote/snippet where available;
- date/version;
- source link;
- why the system thinks this is relevant.

The evidence view can use tables, timelines, and graph views, but the first layer should be legally readable.

### 5. Soovitatud Tegevused

Every workflow should end with a useful action set.

Examples:

- `Paranda eelnõu`
- `Koosta alternatiivne sõnastus`
- `Lisa ülevaatusele`
- `Määra kolleegile`
- `Ava õiguskaardil`
- `Küsi nõustajalt`
- `Ekspordi memo`
- `Lisa seletuskirja mõjuanalüüsi`
- `Märgi kontrollituks`

This is critical: the system should not merely find issues. It should help solve them.

## Analüüsikeskus

`Analüüsikeskus` should be the central page for Section 7 workflows.

It should present legal tasks as large, clear workflow entries, but not as marketing cards. This is an operational tool, so the layout should be dense, scan-friendly, and task-first.

Recommended workflows:

1. `Normi mõjuahel`
2. `EL ülevõtt ja harmoneerimine`
3. `KOV võrdlus`
4. `Pädevused ja järelevalve`
5. `Sanktsioonide võrdlus`
6. `Kohustused, keelud ja halduskoormus`
7. `Kriisi- ja siseturvalisuse õiguskaart`
8. `Avaliku teenuse tervikvaade`

Each workflow should have:

- a one-line legal purpose;
- one primary input field;
- example inputs;
- last used or recommended scope;
- recent analyses;
- a primary action: `Alusta analüüsi`.

## Workflow 1: Normi Mõjuahel

Section 7 use case: before changing a law, the user can see which provisions refer to the changed paragraph, which drafts affect it, and which Supreme Court practice is connected.

### User Intent

The lawyer wants to know what happens if a provision, draft, or idea changes.

### Inputs

- provision reference, such as `AvTS § 35`;
- draft file;
- draft ID;
- natural-language idea;
- selected entity from a report or graph.

### System Logic

The system should automatically:

- resolve the referenced provision or idea to ontology entities;
- find incoming and outgoing references;
- find drafts that touch the same provisions;
- find related court decisions;
- find EU links;
- detect possible conflicts;
- show likely downstream effects;
- rank findings by legal risk and relevance.

### UI Output

The first screen should show:

- `Peamised mõjud`
- `Kõrge riskiga seosed`
- `Seotud eelnõud`
- `Riigikohtu praktika`
- `EL seosed`
- `Soovitatud parandused`

The graph should be available as `Ava õiguskaardil`, not be the default interpretation layer.

### Recommended Actions

- `Koosta mõju memo`
- `Lisa seletuskirja`
- `Koosta alternatiivne säte`
- `Märgi risk ülevaatusele`
- `Ava seotud eelnõu`

## Workflow 2: EL Ülevõtt ja Harmoneerimine

Section 7 use case: directives transposition view helps find which Estonian provisions are connected to a CELEX act and where transposition or harmonization gaps exist.

### User Intent

The lawyer or EU coordinator wants to understand whether Estonian law fully covers an EU obligation.

### Inputs

- CELEX number;
- EU regulation/directive title;
- draft law;
- policy area;
- natural-language question.

### System Logic

The system should:

- resolve CELEX or EU act title;
- list linked Estonian provisions;
- identify relevant draft legislation;
- detect missing or weak links;
- show court practice where relevant;
- compare current law to draft changes;
- highlight obligations, deadlines, and affected institutions if available.

### UI Output

The first result should be a transposition matrix:

- EU article or obligation;
- Estonian provision;
- status: covered, partial, missing, unclear;
- evidence;
- responsible institution;
- recommended action.

### Recommended Actions

- `Täpsusta ülevõtu seos`
- `Lisa puuduv säte`
- `Koosta kooskõla memo`
- `Saada EL koordinaatorile ülevaatuseks`
- `Küsi nõustajalt parandust`

## Workflow 3: KOV Võrdlus

Section 7 use case: KOV regulations can be compared by municipality, issuer, and normalized titles, for example waste-management rules or social-benefit procedures.

### User Intent

The lawyer wants to see how local governments regulate the same topic and whether there are outliers, inconsistencies, or reusable patterns.

### Inputs

- policy area;
- regulation title;
- municipality;
- institution;
- natural-language service or obligation.

### System Logic

The system should:

- normalize KOV regulation titles;
- group similar regulations;
- compare obligations, definitions, procedures, deadlines, and sanctions;
- identify outliers;
- identify common model wording;
- show regional coverage and missing regulation areas.

### UI Output

The result should show:

- comparison table by KOV;
- common clauses;
- divergent clauses;
- missing or unusual provisions;
- recommended harmonization options.

### Recommended Actions

- `Koosta võrdlusmemo`
- `Paku ühtlustatud sõnastus`
- `Märgi erisused ülevaatuseks`
- `Ekspordi tabel`

## Workflow 4: Pädevused ja Järelevalve

Section 7 use case: competence mapping shows which institutions have permit, supervision, procedure, or enforcement tasks.

### User Intent

The lawyer wants to know who is responsible for doing what, whether responsibilities overlap, and whether a draft creates unclear competence.

### Inputs

- institution name;
- policy area;
- draft law;
- task type: license, supervision, enforcement, procedure;
- public service.

### System Logic

The system should:

- extract institutions and task verbs;
- classify competence type;
- find source provisions;
- identify overlaps between institutions;
- identify missing implementing authority;
- identify KOV vs national responsibility;
- compare current law and draft changes.

### UI Output

The result should show a competence map:

- institution;
- task;
- legal basis;
- task type;
- affected area;
- related draft changes;
- risk: overlap, gap, unclear mandate.

### Recommended Actions

- `Täpsusta volitusnorm`
- `Lisa rakendusvastutus`
- `Koosta pädevusmemo`
- `Määra asutusele ülevaatus`

## Workflow 5: Sanktsioonide Võrdlus

Section 7 use case: sanctions index allows comparing fines, penalty payments, imprisonment, and other sanctions by field or legal act.

### User Intent

The lawyer wants to know whether a proposed sanction is proportionate and consistent with similar areas.

### Inputs

- sanction amount or type;
- draft provision;
- legal act;
- policy area;
- violation description.

### System Logic

The system should:

- classify sanction type;
- find comparable sanctions;
- compare amount ranges and severity;
- show legal act and field;
- flag outliers;
- show related court practice where available;
- recommend consistency adjustments.

### UI Output

The result should show:

- sanction comparison matrix;
- severity bands;
- comparable provisions;
- outliers;
- proportionality notes;
- recommended alternative wording.

### Recommended Actions

- `Paku proportsionaalne sanktsioon`
- `Lisa põhjendus seletuskirja`
- `Võrdle valdkonna praktikaga`
- `Saada karistusõiguse ülevaatusele`

## Workflow 6: Kohustused, Keelud ja Halduskoormus

Section 7 use case: deontic classification helps find concentrations of obligations, prohibitions, permissions, and rights, useful for evaluating administrative burden.

### User Intent

The lawyer wants to understand whether a draft adds too many obligations, unclear prohibitions, or administrative burden.

### Inputs

- draft law;
- public service;
- target group;
- policy area.

### System Logic

The system should:

- classify provisions as obligation, prohibition, permission, right, competence, sanction;
- identify affected groups;
- count new or changed obligations;
- compare with current law;
- show burden concentration by target group and institution;
- recommend simplification.

### UI Output

The result should show:

- burden summary;
- obligations by target group;
- obligations by institution;
- new vs existing duties;
- potential duplication;
- simplification suggestions.

### Recommended Actions

- `Vähenda dubleerivat kohustust`
- `Koosta halduskoormuse lõik`
- `Täpsusta adressaat`
- `Lisa erand või üleminekusäte`

## Workflow 7: Kriisi- ja Siseturvalisuse Õiguskaart

Section 7 use case: cross-references and competences can bring crisis, police, rescue, data protection, and local-level norms into one work view.

### User Intent

The lawyer wants a consolidated legal map for a crisis or internal-security scenario.

### Inputs

- scenario, such as cyber incident, flood, evacuation, public-order event;
- institution;
- draft;
- legal act.

### System Logic

The system should:

- map relevant laws, regulations, KOV norms, EU acts, and court practice;
- identify responsible institutions;
- identify emergency powers and limits;
- identify data protection constraints;
- identify conflicting or unclear mandates;
- show time-sensitive legal dependencies.

### UI Output

The result should show:

- scenario map;
- institution responsibility table;
- emergency powers;
- data protection constraints;
- KOV/national split;
- risks and recommended clarifications.

### Recommended Actions

- `Koosta kriisiõiguse memo`
- `Täpsusta pädevusjaotus`
- `Lisa andmekaitse kontroll`
- `Ava õiguskaardil`

## Workflow 8: Avaliku Teenuse Tervikvaade

Section 7 use case: laws, regulations, KOV procedures, court practice, and EU acts regulating a service can be combined into one query instead of searching separate portals manually.

### User Intent

The lawyer wants to understand the full legal environment around a public service.

### Inputs

- public service name;
- institution;
- policy area;
- draft;
- citizen or business obligation.

### System Logic

The system should:

- identify the service;
- find national laws;
- find regulations;
- find KOV procedures;
- find EU obligations;
- find related court practice;
- map responsible institutions;
- show service lifecycle steps;
- identify legal gaps and duplication.

### UI Output

The result should show:

- service legal map;
- process steps;
- legal basis per step;
- responsible institution;
- citizen/business obligations;
- court and EU links;
- risks and improvement suggestions.

### Recommended Actions

- `Koosta teenuse õigusmemo`
- `Paku lihtsustatud regulatsiooni`
- `Lisa mõjuanalüüsi`
- `Saada asutusele ülevaatuseks`

## Eelnõud Workspace

`Eelnõud` should remain the place for actual documents, but it should become more action-oriented.

Current useful parts:

- upload `.docx` or `.pdf`;
- distinguish `Eelnõu` and `VTK`;
- link eelnõu to VTK;
- show processing status;
- show impact report;
- show similar drafts;
- show version history;
- export reports.

Recommended improvements:

- rename the page description from generic upload language to "document workspace";
- show unresolved findings directly in the list;
- show next action per draft;
- show whether analysis is stale after ontology updates;
- show whether draft has unresolved EU, conflict, competence, or burden risks;
- make `Vaata mõjuaruannet`, `Küsi nõustajalt`, and `Paranda koostajaga` primary actions;
- make version comparison more prominent for legislative lifecycle work.

List row should show:

- title;
- type: eelnõu or VTK;
- lifecycle stage;
- analysis status;
- highest legal risk;
- unresolved review count;
- owner;
- last change;
- next action.

## Õiguskaart

The current Explorer should become `Õiguskaart`.

The value of the graph is high, but it should be a contextual evidence view, not the first place a lawyer has to interpret results.

Recommended behavior:

- opened from analysis results with focus already applied;
- shows selected provision/draft/service/institution as the center;
- uses legal filters: laws, drafts, court practice, EU, KOV, competences, sanctions;
- offers predefined views: impact chain, EU links, competence map, version timeline;
- detail panel explains relation meaning in Estonian legal language;
- graph controls use domain words, not simulation words.

Replace controls such as:

- `Taaskäivita simulatsioon`
- `Lülita silte`
- `Rühmita`

With:

- `Näita mõjuahelat`
- `Näita EL seoseid`
- `Näita pädevusi`
- `Näita kohtupraktikat`
- `Näita eelnõusid`
- `Näita ajalist vaadet`

Technical graph controls can remain under `Vaate seaded`.

## Koostaja

The AI drafter should not feel like an isolated wizard. It should be a drafting workspace informed by prior analysis.

Recommended structure:

1. `Kavatsus`
2. `Õiguslik eeluuring`
3. `Lahendusvariandid`
4. `Struktuur`
5. `Sätted`
6. `Kontroll ja mõju`
7. `Eksport`

The most important addition is `Lahendusvariandid`.

Before drafting clauses, the system should propose legal options:

- amend existing law;
- create new act;
- use regulation instead of law;
- add competence rule;
- add exception;
- add transitional provision;
- no legislative change needed;
- policy or administrative measure may be sufficient.

For each option, show:

- when this option is appropriate;
- legal risks;
- affected institutions;
- EU implications;
- drafting effort;
- recommended choice.

The system should ask clarifying questions only in legal terms:

Good:

- "Millist sihtrühma muudatus peamiselt puudutab?"
- "Kas eesmärk on luua uus kohustus või täpsustada olemasolevat?"
- "Kas muudatus peab jõustuma kindlal kuupäeval?"

Bad:

- "Millist ontology classi kasutada?"
- "Kas otsida Jena named graphist?"
- "Millise SPARQL päringu käivitan?"

## Nõustaja

The current `Vestlus` should be renamed or reframed as `Nõustaja`.

It should support natural-language advice, but it should not become the only way to access structured workflows. Lawyers should not need to invent the correct prompt.

Recommended design:

- keep free chat;
- add suggested legal tasks;
- support context-aware questions from any report row;
- let the user ask "Mida ma peaksin parandama?";
- let the system propose actions after answering;
- show sources and confidence;
- create annotations or drafting tasks from an answer.

Suggested commands should map to ministry workflows:

- `/mõjuahel`
- `/el-ülevõtt`
- `/kov-võrdlus`
- `/pädevused`
- `/sanktsioonid`
- `/halduskoormus`
- `/seletuskiri`

The command list should be optional. The UI should also show these as buttons or suggestions in normal language.

## Ülevaatus

Ministry legal work is collaborative. Findings need review, confirmation, and sometimes legal judgment.

`Ülevaatus` should collect:

- unresolved impact findings;
- row-level annotations;
- assigned reviews;
- competence confirmations;
- EU transposition confirmations;
- sanctions/proportionality confirmations;
- draft sections waiting for legal review;
- decisions already marked as accepted, rejected, or fixed.

Recommended review states:

- `Uus leid`
- `Vajab kontrolli`
- `Kinnitatud`
- `Parandus koostamisel`
- `Lahendatud`
- `Ei kohaldu`

Every finding should allow:

- assign to person;
- add note;
- ask advisor;
- open evidence;
- create drafting task;
- mark decision.

## Töölaud

The dashboard should stop being a welcome page and become an operational work queue.

Recommended dashboard sections:

- `Minu järgmised tegevused`
- `Kõrge riskiga leiud`
- `Ülevaatust ootavad eelnõud`
- `Aegunud analüüsid`
- `Uued ontoloogia muudatused`
- `Hiljutised ekspordid`
- `Minu järjehoidjad`

Each item should include a recommended action.

Examples:

- "AI eelnõu mõjuanalüüs valmis. 3 konflikti vajavad ülevaatust. Ava aruanne."
- "EL AI määruse seos on ebaselge. Kontrolli ülevõttu."
- "VTK ootab lahendusvariandi valikut. Jätka koostajas."
- "Ontoloogia uuenes pärast aruande koostamist. Analüüsi uuesti."

## Advice-First Interaction Model

The most important UX shift is that Seadusloome should not merely show data. It should advise.

For every finding, the UI should answer:

1. What is the issue?
2. Why does it matter legally?
3. How serious is it?
4. What evidence supports it?
5. What should the lawyer do next?
6. Can the system draft a possible fix?

Example finding:

> Eelnõu võib luua kattuva järelevalvepädevuse Tarbijakaitse ja Tehnilise Järelevalve Ameti ning Andmekaitse Inspektsiooni vahel.

Advice:

> Soovitus: täpsustada, milline asutus kontrollib läbipaistvuskohustust ja milline isikuandmete töötlemise õiguspärasust. Lisa volitusnormi lõppu eristav lause.

Action:

- `Koosta parandussõnastus`
- `Lisa pädevusmemo`
- `Märgi ülevaatuseks`

## Clarifying Questions

When the system needs clarification, it should ask about legal intent, policy scope, or institutional choice.

Allowed question types:

- policy goal;
- target group;
- affected institution;
- intended legal instrument;
- timeframe;
- risk tolerance;
- EU obligation;
- whether the change creates, removes, or clarifies obligations;
- whether KOV autonomy is affected.

Avoided question types:

- database query choices;
- ontology class choices;
- graph traversal depth;
- prompt-engineering choices;
- model/provider choices;
- embedding or vector search details;
- internal pipeline stage choices.

If a technical decision is necessary, the system should choose a default and explain it in legal terms.

Example:

> Kasutan vaikimisi kehtivat õigust, seotud eelnõusid, Riigikohtu praktikat ja EL õigusakte, sest need on selle mõjuanalüüsi jaoks tavaliselt olulised.

## Result Ranking Logic

Findings should be ranked by legal usefulness, not by raw graph distance.

Suggested ranking criteria:

- direct legal dependency;
- current validity;
- same policy area;
- same target group;
- same institution;
- EU obligation link;
- court interpretation link;
- draft lifecycle relevance;
- severity of conflict;
- likelihood of real-world impact;
- user ministry relevance;
- recent change.

The UI can expose this as:

- `Kõrge risk`
- `Vajab kontrolli`
- `Taustateave`
- `Väike mõju`

Do not expose it as:

- score vector;
- graph weight;
- SPARQL match;
- embedding similarity.

## Evidence and Trust

Legal users will not trust black-box advice without evidence.

Each recommendation should include:

- source;
- quote or snippet where possible;
- relation explanation;
- version/date;
- confidence or status;
- human confirmation status.

Confidence should be framed carefully:

- `Tugev seos`
- `Tõenäoline seos`
- `Vajab kontrolli`
- `Nõrk seos`

Avoid model-like phrasing such as:

- `embedding score`;
- `LLM confidence`;
- `cosine similarity`.

## Visual Design Direction

This should feel like a professional government workbench.

Design direction:

- dense but calm;
- restrained color;
- strong hierarchy;
- clear status and risk coding;
- tables for comparison;
- timelines for version/lifecycle;
- side panels for evidence;
- graph only when relationships matter visually;
- no marketing-style hero sections;
- no oversized decorative cards;
- no playful visual language.

The dark theme can work, but legal analysis screens may benefit from a lighter reading mode later because lawyers read long text and tables for long periods.

## Concrete Page Recommendations

### Analysis Result Page

Recommended layout:

- top: title, input summary, scope, status;
- left/main: findings grouped by legal issue;
- right side panel: recommended actions and evidence preview;
- tabs or segmented control: `Kokkuvõte`, `Tõendid`, `Õiguskaart`, `Ülevaatus`, `Eksport`;
- sticky action bar for `Paranda`, `Ekspordi`, `Määra`, `Küsi nõustajalt`.

### Draft Detail Page

Recommended layout:

- title and lifecycle stage;
- status/processing;
- highest legal risks;
- next recommended action;
- linked VTK and versions;
- analyses;
- unresolved review items;
- exports.

### Impact Report Page

Current sections are useful but should be reframed:

- `Kokkuvõte`
- `Soovitatud tegevused`
- `Kõrge riskiga leiud`
- `Mõjuahel`
- `Konfliktid`
- `EL ülevõtt`
- `Lüngad`
- `Pädevused`
- `Sanktsioonid`
- `Halduskoormus`
- `Tõendid`
- `Eksport`

Not every report needs every section. Empty sections should collapse into a short "no issue found" row.

### Graph Detail Panel

The detail panel should explain:

- what the entity is;
- why it is relevant;
- which workflow surfaced it;
- what action the user can take.

It should not lead with raw URI.

## Implementation Phasing

### Phase A: Navigation and Framing

- Rename or reframe `Vestlus` as `Nõustaja`.
- Add `Analüüsikeskus`.
- Reframe `Uurija` as `Õiguskaart`.
- Rewrite dashboard as a work queue.
- Fix Estonian diacritics in UI copy.

### Phase B: Analysis Center MVP

Implement workflow shells first, even if some reuse existing analysis backend:

- `Normi mõjuahel`;
- `EL ülevõtt`;
- `Pädevused`.

These align closely with existing affected entities, EU links, and graph relationships.

### Phase C: Expanded Section 7 Workflows

Add:

- `KOV võrdlus`;
- `Sanktsioonide võrdlus`;
- `Halduskoormus`;
- `Avaliku teenuse tervikvaade`;
- `Kriisiõiguse kaart`.

These may require additional ontology enrichment and extraction, but the UI structure can be introduced earlier.

### Phase D: Advice and Solution Layer

For each finding:

- generate legal explanation;
- propose solution options;
- draft suggested wording;
- link to evidence;
- allow review/assignment.

### Phase E: Review Operations

Add `Ülevaatus`:

- review queue;
- assigned findings;
- status transitions;
- ministry confirmations;
- exportable decisions.

## Success Criteria

The new UI is successful if a ministry lawyer can:

- start with a legal question, not a technical tool;
- get a useful first analysis without prompt engineering;
- see recommended legal actions;
- inspect evidence quickly;
- ask for proposed wording;
- assign findings to review;
- export a memo or report;
- understand how a draft affects law, EU obligations, KOV rules, institutions, sanctions, and administrative burden.

The product should feel less like "search plus graph plus chat" and more like:

> A legal analysis and drafting workbench that uses the Estonian legal ontology to advise ministry lawyers during law creation.

