# Applied workflow experiment

2026-11-21

The experiment tests whether a single sqrlly workflow model can carry four kinds of work from intake to a result someone can inspect. The branch adds reusable run inputs, correlated execution logs, and workflow examples for app building, CRM operations, public health analysis, and research synthesis. Each example couples flexible orchestration with checks that run outside the model.

## What the references show

| Use case | Observed in the references | What sqrlly can exercise |
| --- | --- | --- |
| SMB app building | Samus keeps project chat, build runs, progress, artifacts, and deployment in an application around the builder. Lovable exposes planning as a separate activity before editing. [Lovable plan mode](https://docs.lovable.dev/features/plan-mode) | Accept a brief, isolate generated changes, verify the merged result, and return a preview and artifact index to a caller. A builder can use those outputs in its own project view. |
| CRM operations | Clarify starts jobs from record events, schedules, and webhooks. Its API uses object-specific unique fields, explicit upserts, and relationship operations. [Workflow guide](https://developer.clarify.ai/docs/guides/build-a-workflow), [Import guide](https://developer.clarify.ai/docs/guides/import-crm-data) | Accept a batch, normalize and deduplicate it, attach enrichment evidence, and apply an approved diff. A caller owns event ingestion, API credentials, and delivery acknowledgments. |
| Data science | CDC PLACES documents geography, modeled estimates, and release differences. Shark Repellent's `11ty-base/.eleventy.js` separates Markdown presentation into layouts and a static build. GitHub Pages can publish the output of a separate build job. [CDC release notes](https://www.cdc.gov/places/current-release-notes/index.html), [CDC methodology](https://www.cdc.gov/places/methodology/index.html), [GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages) | Pass source metadata and files through ETL, quality checks, exploratory analysis, cleaned data, and rendered reports. The same Markdown can be reviewed locally and built in CI. |
| Research synthesis | Cochrane calls for a predefined protocol, study level extraction, and a process for resolving reviewer differences. PRISMA supplies reporting checklists and flow diagrams. [Cochrane review questions](https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-01), [Cochrane extraction](https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-05), [PRISMA 2020](https://www.prisma-statement.org/prisma-2020) | Keep the question, search, eligibility decisions, extractions, and computed results as separate handoffs. Draft text can cite the frozen evidence and report uncertainty from the analysis file. |

The useful common boundary is a workflow invocation with explicit inputs, executable checks, and inspectable files. Authentication, tenant state, scheduling, external delivery, and study methodology already have owners in the surrounding systems. A sqrlly node can call their APIs through an adapter as the workflow needs them.

## Two core changes

### Inputs belong to the invocation

Repeated `--var NAME=VALUE` options supply strings at `vars.NAME`. A caller can reuse a YAML file for another customer, batch, or input path, without rendering a new YAML document. A command receives the rendered value as one argument when the workflow uses argv form. A child receives only the values the caller maps through `workflow.with`. Routes and evals can refer to the same top-level values.

Resume reuses checkpointed input values. Accepting a changed value while retaining completed nodes would make their old outputs look current. A fresh run uses the new values, ideally with a separate work directory for a separate request. File contents can still change at the same path. When reviewing a run, preserve the input bytes or digest, and explicitly replay the producer and its dependents if the file changed. Use the environment or a secret store for credentials.

The alternatives were embedding data in YAML, passing environment through a separate convention, and introducing declared typed inputs. YAML edits obscure which variables changed and complicate reusing the same workflow. Environment variables can stay appropriate for credentials and process setup. The experiment uses strings because identifiers and paths cover these examples. Typed input declarations would need a concrete requirement for defaults, validation, or generated intake forms. [Issue 5](https://github.com/christopherseaman/sqrlly/issues/5)

### Each invocation has an observable outcome

CLI JSONL records bear an `invocation_id`. A fresh run and its resume can append to the same file while keeping distinct event sets. `workflow_end.status` shows `succeeded`, `failed`, or `interrupted`, including failures with no node records. `workflow_start.dry_run` distinguishes a trace. Existing node events, scoped child IDs, and failure kinds remain usable.

A caller can launch the CLI in a dedicated work directory, retain its process exit code, tail JSONL, and retrieve artifacts using the workflow's named outputs. A missing end event is an incomplete observation. The launcher still needs to detect worker loss and decide whether to resume. The invocation ID identifies a process attempt. An external system should assign its own durable request ID across attempts and keep its artifact association. Logs can include eval text and errors, so access to them should follow the input data's access rules. [Issue 50](https://github.com/christopherseaman/sqrlly/issues/50)

## Four workflow slices

### SMB quote builder

The offline path validates a quote plan with JSON Schema, fans out work to isolated Git worktrees, and checks the promoted files together. This exercises the same plan and integration boundary that a larger app would use. The preview and artifact manifest give a caller paths it can present. The companion path takes a written brief, puts generated browser files through a deterministic acceptance check, and creates a local preview from the accepted files. [Run the SMB examples](../../examples/experimental/smb_app/README.md)

A service for small businesses would persist projects and ownership outside the runner. The first integrated product test would accept a change request on an existing app, retain the accepted baseline, and build a preview for review. A separate deployment job would consume a reviewed commit and manage secrets, migrations, tenancy, and rollback. A failed or conflicting edit should keep the prior preview available. This experiment's acceptance test checks a narrow quote feature and its local handoff.

### CRM enrichment and review

The CRM path keeps incoming IDs, normalized records, and enrichment evidence in separate files. It prepares a proposal before applying writes. The previewed payload is bound to a digest, so approval must match the content that will be applied. A SQLite transaction provides the local write boundary and records application for replay. [Run the CRM example](../../examples/experimental/crm_ops/README.md)

A live adapter should preserve both the source event identity and the target's unique keys. Enrichment needs a source and retrieval time so that users can distinguish enrichment from verified CRM facts. The adapter should retry transport failures within a budget, respect pagination and provider limits, and reconcile writes after a lost connection. A completed sqrlly node records local execution success. The remote system must still provide stable upsert or idempotency semantics for a retry. Operators need an authenticated approval record if the result has business consequences. [Clarify API import guide](https://developer.clarify.ai/docs/guides/import-crm-data)

### Public health data and publishing

The data path records source identity, stages tabular data, reports missingness, cleans the analysis input, and renders a chart and Markdown. Eleventy builds the resulting report with the same separation of content and layout used by the local reference. [Run the health example](../../examples/experimental/health_data/README.md), [Eleventy configuration](https://www.11ty.dev/docs/config/)

At larger scale, store input bytes and intermediate tables outside Git snapshots. Use a digest and dataset version as the handoff, and stream or partition work by geography and measure. Send artifact paths and small summaries through workflow state. Persist full tables outside node stdout to avoid serializing them into repeated checkpoints. A production ingestion step should verify headers, units, geography, and release before it publishes a table. PLACES rates represent modeled estimates by area. Summaries of those rates require the measure type, missingness policy, and geographic coverage. The workflow report preserves those distinctions. [CDC methodology](https://www.cdc.gov/places/methodology/index.html)

CI can run the data stages and Eleventy in a clean checkout, upload only the generated site, and hold deployment in a protected environment. Inputs and reports need retention independent of the published website so an earlier article can be reconstructed. [GitHub Pages build and deploy jobs](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)

### Evidence synthesis and manuscript

A real review starts with a plain English question and an exploratory publication check. A reviewer refines the population, intervention, comparator, and outcomes, records eligibility and an analysis plan, and approves a database-specific search strategy. After that decision, retain the exact translated queries, dates, raw responses, and stable record IDs. An exploratory search and the final search serve different purposes. If the protocol changes, keep the original and the amendment alongside the revised screening record. The example starts with a written question and protocol, then reads a frozen synthetic search result. [Cochrane protocol guidance](https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-01), [Cochrane searching and selecting studies](https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-04)

Screening and extraction can repeat by stable publication ID. Multiple reports may describe the same study, so statistical analysis should group by study after adjudication. Record reasons for exclusion, missing full texts, measurement choices, and source locations for every extracted value. Use deterministic scripts for counts, estimators, and figures. A manuscript writer can then take its numeric claims from those artifacts, and a reviewer can trace each claim back to them. The example exercises a human decision handoff, PRISMA arithmetic, a toy estimate with uncertainty, and a verified draft. [Run the synthesis example](../../examples/experimental/research_synthesis/README.md), [Cochrane data extraction](https://www.cochrane.org/authors/handbooks-and-manuals/handbook/current/chapter-05), [PRISMA flow diagram](https://www.prisma-statement.org/prisma-2020-flow-diagram)

## Priorities after the experiment

| Priority | Change | Evidence needed |
| --- | --- | --- |
| 1 | A review state using the planned human pause extension. CRM and synthesis currently gate on operator files and resume from a checkpoint. | Missing approval yields a durable waiting state. Approval binds to the exact artifact version. Rejecting or changing the artifact leaves external state untouched. [Issue 19](https://github.com/christopherseaman/sqrlly/issues/19) |
| 2 | A common artifact descriptor at the boundary between a run and its caller. Start with names, relative paths, hashes, media types, and producing nodes. | An app controller and a publishing workflow can retrieve the exact accepted artifact without reading SQLite or interpreting terminal output. |
| 3 | A launcher for queued or hosted work if SAMUS or a CRM adapter needs one. Use a workspace per request and preserve the external request ID across invocations. | Two users can run the same workflow concurrently with separate files, permissions, and lifecycle controls. Cancellation and recovery preserve the accepted version. |
| 4 | Domain adapters with real acceptance fixtures. A Clarify sandbox can test upsert and partial failure. A science workflow can verify a realistic data volume. A research team can adjudicate a real extraction sample. | Each adapter demonstrates correctness at its real boundary. Reuse is justified by a second workflow requesting the same adapter behavior. |

The first two changes are shared across all four cases. Domain experiments should drive their interface. A shared web service or deployment layer would need an actual caller, an authorization model, and a target environment.

## Verification

| Check | Observed result |
| --- | --- |
| SMB quote builder | The two isolated workers produced the expected quote lines. The joined check accepted both lines and the preview manifest named three artifacts. The separate authenticated Codex run generated a browser app, passed the repository quote check and its own Node tests, and handed off HTML and its JavaScript module. |
| CRM approval and replay | Wrong approval and target edits halted writeback. The approved run produced two contacts and two enrichment rows. A second apply found one ledger record and left the row counts unchanged. |
| Health analysis | The synthetic fixture produced 10.0% and 15.0% mean county estimates. Eleventy published both HTML and its SVG. A real three row CDC PLACES sample confirmed the CSV field mapping. A separate synthetic 50,000 row input completed the seven data steps in 15.22 seconds, with a maximum RSS of 94,312 KiB reported by GNU `time` on the experiment host. |
| Synthesis and review | Missing and incomplete decisions prevented manuscript generation. With reviewed synthetic records, the flow reconciled four identified, one duplicate, one excluded, and two included. The fixed effect result was 3.00 wellbeing points, with 95% confidence limits of 1.61 and 4.39. |
| CLI integration | Tests cover literal arguments and empty strings, explicit child inputs, scope in evals and routes, saved variables on resume, and grouped log outcomes after early failure, node failure, interruption, and successful replay. |

The data timing describes this local run and input shape. Larger inputs, hosted deployment, CRM writes, and review with real source publications have separate acceptance boundaries.
