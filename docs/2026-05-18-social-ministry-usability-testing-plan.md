# Social Ministry Lawyer Usability Testing Plan

Date: 2026-05-18

Purpose: define realistic end-to-end test stories for validating the Seadusloome UI and usability from the perspective of a lawyer working in the Ministry of Social Affairs.

Primary persona: ministry lawyer whose daily work is to prepare VTK impact analyses, review draft laws, analyse EU legal developments, draft new Estonian legislation, and explain legal impact to colleagues and leadership.

## 1. What This Plan Tests

This is a usability and workflow test plan. It is not a unit-test plan and it is not a technical ontology validation plan.

The test asks whether a Social Ministry lawyer can use Seadusloome to:

1. create impact analyses for VTKs (`väljatöötamiskavatsus`);
2. read law drafts (`eelnõud`) and assess their impact on Estonian and EU law;
3. analyse new EU directives, regulations, and other EU legal acts and understand their impact on Estonian law;
4. draft new law drafts for the Social Ministry and assess their impact on other laws.

The main usability question:

> Can the lawyer complete these legal tasks without understanding technical concepts such as RDF, SPARQL, named graphs, embeddings, graph URIs, vector indexes, or ontology predicates?

## 2. Test Method

Run moderated usability sessions with realistic legal-policy tasks. The participant should think aloud while using Seadusloome. The moderator should avoid teaching the product unless the participant is blocked.

Recommended session length: 75-90 minutes.

Recommended participants:

- 2-3 lawyers who prepare or review Social Ministry legislation;
- 1 policy adviser who contributes to VTKs;
- 1 senior reviewer or department lead who consumes impact summaries;
- optionally 1 EU law specialist.

Recommended environment:

- Estonian UI;
- ministry-lawyer role, not admin;
- realistic but non-sensitive sample documents;
- ontology and EU-law data loaded enough to return visible evidence;
- seeded draft workspace with at least one VTK, one draft law, one EU directive, and one existing impact report.

## 3. Cross-Workflow Success Criteria

A test story is successful when the participant can:

- identify where to start without moderator instruction;
- describe the result in ordinary legal language;
- find the legal basis and source evidence behind a finding;
- understand confidence, risk, and recommended next action;
- move between `Töölaud`, `Analüüsikeskus`, `Eelnõud`, `Õiguskaart`, `Koostaja`, and `Nõustaja` without losing context;
- export, save, annotate, or continue the work when the task naturally requires it;
- avoid technical detours and raw data surfaces unless explicitly needed.

Failure signals:

- the participant asks what module to use before starting a normal legal task;
- the participant sees a result but cannot explain why the system found it;
- the participant cannot tell whether the result concerns Estonian law, EU law, court practice, a draft, or a historical version;
- the participant cannot find source links or evidence;
- the participant is forced to choose technical options;
- the participant loses the draft, VTK, or EU act context while navigating;
- the system gives findings but no next action.

## 4. Test Data Set

Use Social Ministry-adjacent examples that are realistic but safe for testing.

Recommended sample topics:

- sickness benefit or incapacity-for-work benefit changes;
- social welfare service eligibility;
- disability benefit application process;
- health data access or processing;
- labour-market support measure;
- child protection or family benefit amendment;
- EU directive affecting accessibility, platform work, equality, health data, social security coordination, or working conditions.

Each sample should include:

- one short natural-language policy intent;
- one VTK-like document;
- one draft-law document;
- one EU legal act identified by CELEX number or title;
- at least three affected Estonian acts;
- at least one related EU act;
- at least one court-practice or legal-interpretation item if available;
- at least one plausible legal conflict or gap.

## 5. Moderator Checklist

For every story, observe:

- starting point: where the participant goes first;
- time to first meaningful result;
- whether labels match the participant's legal vocabulary;
- whether the participant understands the scope of analysis;
- whether the participant trusts the evidence and can inspect it;
- whether the recommended actions are useful;
- where the participant hesitates, backtracks, or asks for clarification;
- whether the participant notices missing or surprising legal relationships;
- whether the participant can produce an artefact usable in ministry work.

Record severity:

- `P0`: participant cannot complete a core legal workflow.
- `P1`: participant completes the task only with moderator help or with serious confusion.
- `P2`: participant completes the task but hits friction, unclear labels, or missing affordances.
- `P3`: minor wording, layout, or efficiency issue.

## 6. Test Stories

### Story 1 - Start a VTK Impact Analysis from Policy Intent

As a Social Ministry lawyer, I want to describe a planned policy change in plain language and receive an initial VTK impact analysis so that I can decide whether the idea is legally feasible before drafting.

Example prompt:

> Soovime lihtsustada puudega inimese toetuse taotlemist nii, et osa andmeid liiguks automaatselt Tervisekassast ja Töötukassast.

Steps:

1. Start from `Töölaud` or `Analüüsikeskus`.
2. Enter the policy intent in natural language.
3. Confirm or adjust the analysis scope in legal terms.
4. Review affected Estonian acts, EU law, rights/obligations, data-protection implications, and possible conflicts.
5. Open evidence for one high-risk finding.
6. Ask `Nõustaja` a follow-up question about the finding.
7. Save the result as a VTK analysis draft.

Expected outcome:

- the system recognises the topic and proposes a sensible scope;
- the lawyer sees affected laws, relevant EU law, risk level, rationale, and recommended next action;
- evidence opens in context, not as a raw graph;
- the saved result can be continued later from the dashboard.

Key usability risks:

- the UI asks the user to choose technical analysis type;
- the result is only a list of legal acts without explaining legal impact;
- the user cannot tell what should be done next.

### Story 2 - Upload an Existing VTK and Generate Impact Findings

As a lawyer, I want to upload a VTK document and have Seadusloome extract the legal objects and impact areas so that I do not manually search all affected acts.

Steps:

1. Go to `Eelnõud`.
2. Upload a VTK document.
3. Watch processing status and confirm that the document type is recognised as VTK.
4. Review extracted legal references, topics, affected groups, and proposed scope.
5. Run impact analysis.
6. Inspect high-risk findings and evidence.
7. Add an annotation to one finding.

Expected outcome:

- upload state and processing state are clear;
- VTK is distinguished from draft law;
- extracted references are editable or confirmable;
- impact findings are grouped by legal relevance, not technical source;
- annotation is attached to the finding and visible later.

### Story 3 - Compare VTK Solution Options

As a lawyer, I want to compare two regulatory options in a VTK so that I can explain which option has lower legal risk and administrative burden.

Steps:

1. Open a saved VTK analysis.
2. Add two policy options.
3. Run impact comparison.
4. Compare affected laws, EU obligations, institutional duties, data flows, sanctions if relevant, and implementation burden.
5. Export or copy the comparison summary for internal discussion.

Expected outcome:

- the UI makes options easy to compare side by side;
- the system explains trade-offs in legal and administrative terms;
- the participant can identify which option is legally safer and why.

### Story 4 - Prepare a VTK Analysis Summary for Leadership

As a lawyer, I want to turn a VTK impact analysis into a concise summary so that a department lead can decide whether the policy idea should proceed.

Steps:

1. Open a completed VTK analysis.
2. Review the executive summary, risk level, affected legal areas, and unresolved questions.
3. Check source evidence for one important claim.
4. Export the summary or prepare it for sharing.

Expected outcome:

- the summary is decision-oriented;
- high-risk issues are visible before details;
- evidence remains available but does not overwhelm the summary;
- export is understandable outside Seadusloome.

### Story 5 - Review a Draft Law Against Existing Estonian Law

As a lawyer, I want to read an uploaded draft law and see how it affects current Estonian law so that I can identify conflicts, amendment targets, and missing transitional rules.

Steps:

1. Open `Eelnõud`.
2. Select or upload a draft law.
3. Review document metadata, extracted provisions, and current processing status.
4. Run or open the impact report.
5. Inspect affected provisions, conflicts, gaps, and amendment chains.
6. Open `Õiguskaart` from one finding to see the relationship in context.

Expected outcome:

- the participant can quickly find the impact report;
- the report distinguishes affected provisions, conflicts, gaps, and related drafts;
- the evidence map opens focused on the selected finding;
- legal relationship labels are understandable.

### Story 6 - Assess Draft Law Compatibility with EU Law

As a lawyer, I want to check whether a draft law is compatible with relevant EU law so that I can avoid incomplete or incorrect transposition.

Steps:

1. Open a draft law impact report.
2. Find the EU law section.
3. Review related directives, regulations, transposition status, deadlines, and missing provisions.
4. Open one EU act in `Analüüsikeskus`.
5. Ask `Nõustaja` how the draft should be adjusted.
6. Save or annotate the recommendation.

Expected outcome:

- EU findings are not hidden inside general impact findings;
- CELEX numbers, titles, deadlines, and transposition status are visible;
- the user can move from draft-level analysis to EU-act-level analysis;
- recommendations are tied to evidence.

### Story 7 - Review Another Ministry's Draft from a Social Ministry Perspective

As a Social Ministry lawyer, I want to review a draft prepared by another ministry and identify its effect on Social Ministry responsibilities, target groups, benefits, services, and institutions.

Steps:

1. Start from `Töölaud` review queue or `Eelnõud`.
2. Open a draft assigned for review.
3. Filter or focus findings relevant to Social Ministry.
4. Inspect affected Social Ministry acts, agencies, target groups, and obligations.
5. Write an annotation or review comment.
6. Mark the review outcome as no issue, issue found, or needs discussion.

Expected outcome:

- ministry-relevant findings are easy to isolate;
- the participant does not need to read unrelated legal areas first;
- review decision and comment are saved as part of the workflow.

### Story 8 - Track Changes Between Draft Versions

As a lawyer, I want to compare impact analysis results between draft versions so that I can see whether a new version fixed or introduced legal risks.

Steps:

1. Open a draft with at least two versions.
2. Compare the current version with the previous version.
3. Review added, removed, and changed findings.
4. Inspect one newly introduced risk.
5. Save a note for the drafting team.

Expected outcome:

- version history is visible and understandable;
- changes in legal impact are highlighted;
- the user can distinguish document changes from ontology/data updates.

### Story 9 - Analyse a New EU Directive by CELEX Number

As a lawyer, I want to enter a CELEX number for a new EU directive and see which Estonian laws may need amendment so that the ministry can plan transposition work.

Steps:

1. Open `Analüüsikeskus`.
2. Choose EU transposition or enter the CELEX number directly in global search.
3. Review directive metadata, deadline, subject area, and known transposition links.
4. Review affected Estonian acts and provisions.
5. Identify missing, partial, or unclear transposition areas.
6. Create a follow-up task or draft workspace.

Expected outcome:

- CELEX input works without requiring exact title;
- deadline and transposition status are visible early;
- the output separates covered, partial, missing, and unclear areas;
- the lawyer can create a next action from the result.

### Story 10 - Analyse the Estonian Impact of a New EU Regulation

As a lawyer, I want to analyse a directly applicable EU regulation so that I can identify whether Estonian law needs supporting rules, competence changes, sanctions, or procedural amendments.

Steps:

1. Enter the EU regulation title or CELEX number.
2. Confirm that the legal act is a regulation, not a directive.
3. Review affected Estonian laws, institutions, procedures, sanctions, and possible national discretion.
4. Ask for recommended Estonian follow-up measures.
5. Save the analysis to a Social Ministry workspace.

Expected outcome:

- the system does not treat every EU act as a directive transposition task;
- national implementation needs are explained separately from direct applicability;
- institutional and sanction implications are visible where relevant.

### Story 11 - Build an EU Implementation Action Plan

As a lawyer, I want to convert EU-law analysis into an action plan so that I can coordinate drafting responsibilities and deadlines inside the ministry.

Steps:

1. Open a completed EU-law analysis.
2. Review required Estonian actions and deadlines.
3. Assign or note responsibility areas.
4. Create linked draft-law or VTK work items.
5. Export or share the action plan.

Expected outcome:

- action items are specific enough to use in ministry planning;
- each action links back to legal evidence;
- deadlines are not buried in detailed tables.

### Story 12 - Detect EU-Driven Conflicts in Existing Drafts

As a lawyer, I want to see whether a new EU act affects existing draft laws in our workspace so that we do not proceed with outdated drafting assumptions.

Steps:

1. Open a new EU act analysis.
2. Look for related active drafts and VTKs.
3. Open one affected draft.
4. Review whether the EU act changes the draft's risk level.
5. Add an annotation or create a follow-up review task.

Expected outcome:

- existing drafts are connected to the EU analysis;
- the user can navigate directly from EU act to affected draft;
- changes in risk are clear and actionable.

### Story 13 - Start Drafting a New Law from Legislative Intent

As a lawyer, I want to start a new draft law from a legislative intent so that Seadusloome helps structure the draft and asks legally relevant clarification questions.

Steps:

1. Open `Koostaja`.
2. Describe the legislative intent.
3. Answer clarification questions.
4. Review suggested legal structure and affected existing laws.
5. Choose whether to continue as amendment act, new act, or VTK.

Expected outcome:

- the drafter asks legal questions, not technical configuration questions;
- suggested structure is plausible for Estonian legislative drafting;
- the user understands why the system recommends a drafting path.

### Story 14 - Use Ontology Research During Drafting

As a lawyer, I want the drafting workflow to show relevant existing law, EU law, and court practice before clause drafting so that the draft starts from the correct legal context.

Steps:

1. Continue from a drafting intent.
2. Review the ontology research step.
3. Inspect grouped findings: affected provisions, EU acts, court practice, similar drafts, concepts, and institutions.
4. Open one item in `Õiguskaart`.
5. Ask `Nõustaja` whether the finding requires changing the draft structure.

Expected outcome:

- research findings are grouped by legal relationship type;
- counts alone are not treated as analysis;
- the user can inspect evidence without leaving the drafting flow permanently.

### Story 15 - Draft Clauses and Run Iterative Impact Checks

As a lawyer, I want to draft clauses and immediately check their legal impact so that conflicts are found before the draft is exported.

Steps:

1. Generate or write draft clauses.
2. Run impact analysis on the current draft version.
3. Review conflicts, missing amendments, EU issues, and implementation gaps.
4. Revise one clause.
5. Run the analysis again and compare changes.

Expected outcome:

- impact checking is part of drafting, not a separate hidden step;
- the system preserves draft versions;
- the user can see whether a revision reduced risk.

### Story 16 - Export a Draft Law with Impact Evidence

As a lawyer, I want to export a draft law and supporting impact evidence so that I can continue the formal ministry process outside Seadusloome.

Steps:

1. Open a drafted law in `Koostaja` or `Eelnõud`.
2. Review final risk summary and unresolved issues.
3. Export the draft law as `.docx`.
4. Export or attach the impact report.
5. Confirm that evidence links, annotations, and version metadata remain available in Seadusloome.

Expected outcome:

- export is clearly separated from delete/archive actions;
- the exported document is usable for formal drafting work;
- impact evidence remains traceable after export.

### Story 17 - Use Nõustaja as an Embedded Legal Adviser

As a lawyer, I want to ask follow-up questions from inside any analysis result so that I do not need to restart context in a separate chat.

Steps:

1. Open a VTK, draft, or EU-law analysis.
2. Click `Küsi nõustajalt` from a specific finding.
3. Ask why the finding matters.
4. Ask what amendment or drafting option would reduce the risk.
5. Open the cited source from the answer.

Expected outcome:

- chat starts with the selected finding context;
- citations are visible;
- chat answer links back to analysis workflows and evidence map;
- the user does not need to paste legal references manually.

### Story 18 - Use Töölaud as the Daily Legal Work Queue

As a lawyer, I want the dashboard to show the legal work that needs my attention so that I can continue active VTKs, draft reviews, EU-law follow-ups, and unresolved findings.

Steps:

1. Log in as a Social Ministry lawyer.
2. Review `Töölaud`.
3. Open one stale analysis, one assigned review, and one high-risk finding.
4. Return to `Töölaud`.
5. Confirm completed or reviewed items no longer appear as unresolved.

Expected outcome:

- dashboard is task-oriented, not module-oriented;
- items explain why they need attention;
- returning to the dashboard preserves the user's sense of progress.

## 7. End-to-End Testing Paths

Run at least four full paths, one for each main duty.

Path A: VTK impact analysis

1. Describe policy intent.
2. Create VTK analysis.
3. Inspect affected law and EU implications.
4. Ask adviser follow-up.
5. Save and export leadership summary.

Path B: Draft-law review

1. Upload or open draft law.
2. Run impact report.
3. Inspect Estonian-law and EU-law findings.
4. Annotate one issue.
5. Compare with another version or create review outcome.

Path C: New EU law analysis

1. Enter CELEX number.
2. Review transposition or implementation status.
3. Identify affected Estonian law.
4. Create action plan or linked draft workspace.
5. Check affected existing drafts.

Path D: Draft new law

1. Start in `Koostaja`.
2. Answer clarification questions.
3. Review ontology research.
4. Generate clauses.
5. Run iterative impact check.
6. Export draft and impact evidence.

## 8. UI and Usability Questions to Answer

During testing, collect answers to these questions:

- Do ministry lawyers understand the top-level navigation labels?
- Is `Analüüsikeskus` the right place to start legal analysis, or do users expect to start from `Töölaud`?
- Can users distinguish VTK, draft law, EU-law analysis, and advisory chat outputs?
- Are risk levels and confidence labels legally meaningful?
- Are source links and evidence visible enough to build trust?
- Do users understand `Õiguskaart` as evidence context rather than a primary graph tool?
- Are recommendations specific enough to support legal drafting decisions?
- Does the system ask too many or too few clarification questions?
- Can users recover if document parsing or entity extraction is wrong?
- Can users finish with a concrete work product: summary, annotation, action plan, draft, or export?

## 9. Open Questions Before Running Real Sessions

These are not blockers for drafting the plan, but they should be resolved before moderated testing:

- Which real Social Ministry topics may be used safely as non-sensitive test material?
- Should test artefacts be entirely fictional, anonymised from real work, or based on already public drafts?
- Which EU-law examples are most relevant for the first test round?
- Should participants test only Estonian UI copy, or also English terms used in internal technical documentation?
- What counts as an acceptable exported artefact for ministry use: short summary, full impact report, `.docx`, or all of them?

## 10. Definition of Done for the Usability Test Round

The round is complete when:

- at least one participant has attempted each of the four main duty paths;
- every test story has either been run or intentionally deferred;
- all P0/P1 usability blockers are documented with screenshots or reproduction notes;
- wording problems are captured with the exact confusing label or sentence;
- missing evidence or broken context handoffs are listed by workflow;
- the product team has a prioritised fix list for the next UI iteration.
