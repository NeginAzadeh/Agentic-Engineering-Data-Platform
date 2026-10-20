# Fully local Ollama Agent

The repeatable evaluation runner is documented in [Local Agent evaluation](agent_evaluation.md). It wraps this Agent without changing its prompts, tools, recovery rules, or security boundary. The guide records operational scores separately from factual problems found in generated answers; previous smoke-test successes below are not a certification of prose accuracy.

## Targeted answer grounding

The `feature/agent-answer-grounding` changes address factual errors recorded in that evaluation. The evaluation runner and dataset stay unchanged; the read-only tool evidence and Agent answer checks improve. The original baseline reports remain intact.

| Claim | Supporting source and meaning |
| --- | --- |
| Successful pages fetched | Stored `ingestion_runs.pages_fetched`, now exposed for the latest and contributing runs; unknown remains unknown |
| Pagination completeness | Stored `pagination_complete` plus `stop_reason`; contributing PR-run completeness can differ from the latest attempt |
| Starting API URL | `source_start_url`, an explicit alias of retained `source_url`; a `page` parameter proves neither page count nor completeness |
| Extraction and observation timestamps | Latest run's UTC extraction timestamp, earliest/latest PR observation timestamps, and per-PR observation timestamps |
| Extraction freshness | Existing elapsed hours from extraction to the explicit UTC check time; Python divides by 24 for the displayed days |
| Open PR age | Existing creation-to-extraction age; unaffected by today's clock |
| Later GitHub events, review causes, or developer productivity | Unsupported by this evidence; staleness, counts, and author attribution do not establish these claims |

`answer_grounding.py` formats critical metadata from returned scalar fields and provides narrow checks used by the existing final-answer validator. Python's evidence appendix includes each repository's observation range, latest extraction and check timestamps, pages fetched, completeness, stop reason, contributing-run metadata, freshness hours/days, and fixed interpretation limits. For example, a stored age of 245 hours is displayed as `245.00 hours (10.21 days)`. This is an illustrative conversion, not a hardcoded source age. Missing or future extraction ages remain unknown; no replacement timestamp is invented.

Numerical freshness presentation is reserved for Python. The model-facing tool view explicitly omits numerical extraction ages, the per-result `stale_after_hours` threshold, check timestamps, and API starting URLs, while full validated results remain in `tool_calls` for evaluation and the deterministic appendix. Repository/PR links, extraction timestamps, KPIs, PR ages, page counts, and quality flags remain visible to the model. Generic tool-schema policy wording is not an observation of extraction age. This is a deliberate presentation contract, not silent truncation or evidence loss. Both the original result and projected view are subject to the existing tool-result size cap.

If model prose contains a recognized numerical extraction-age statement, even a correct conversion, the validator requests a rewrite without those numbers. A correctly quoted configured freshness-policy threshold is allowed; it is distinct from actual extraction age. A lower-bound statement such as "older than the threshold" is also allowed only when its value/unit matches the configured threshold and the returned stale flag supports it. Metrics such as merge duration and PR age can still appear in model prose. Page-count and recognized completeness claims are checked against explicit returned metadata, including negated statements and false flags; neither a starting URL nor a page count proves the final page index. Recognized ISO extraction/observation timestamps are checked against returned timestamps, with explicit repository identifiers narrowing their scope.

Additional guards target the observed positive claims that stale observations imply old/open PRs or later closure/merge, that PR counts/authorship enable productivity judgments, or that review/bottleneck causes are known. Safe disclaimers remain allowed. A fixed Python-rendered explanation always states the distinction between freshness and open age, and that developer productivity and review causes cannot be inferred.

These checks use the existing shared recovery budget (maximum two) and tools-disabled answer rewrite. `answer_rejected` records every rejected draft and reason, including after the budget is exhausted. Rejected text is labelled `untrusted_draft` in the local trace/report for diagnosis; it is never replayed as an instruction in recovery. Repeated failures yield `answer_withheld`; the Agent never replaces a rejected draft with an invented answer or bypasses tools to run SQL/code. The four tools, validation, read-only DuckDB connections, and model/tool-round budgets remain unchanged.

The guards are conservative pattern checks, not a semantic proof. Complex or unsupported phrasing, aliases instead of explicit repository names, Markdown layouts, quoted titles, and missing context can evade checks or cause a valid draft to be rejected. Numeric KPI claims, comparison reasoning, prose-only date formats, omitted context, and causal/productivity paraphrases still need human review. A correct deterministic appendix does not certify model prose. See [the follow-up evaluation record](agent_evaluation.md#answer-grounding-follow-up) for actual successes and failures.

## Setup and commands

This milestone uses Windows 11, Python 3.12 in the existing `.venv`, Ollama 0.40.2, and the already-installed `qwen3:4b-instruct` tag. It requires the existing engineering DuckDB warehouse. No packages, frameworks, model downloads, API keys, billing accounts, or paid services are added.

Verify your existing setup:

```powershell
ollama --version
ollama list
```

The exact `qwen3:4b-instruct` tag must be present locally. Start the Ollama application if needed, or run `ollama serve` in a separate terminal. Do not run a second server if the application is already serving port 11434. The project neither starts a background service nor downloads models automatically.

From the project root:

```powershell
.\.venv\Scripts\python.exe -m src.engineering_intelligence.local_agent --trace
```

Type a question naming a repository; `exit`, `quit`, Ctrl+C, or end-of-input stops cleanly. Questions are independent, so follow-ups must repeat repository identifiers. UTF-8 CLI output handles Unicode model responses on Windows.

For a single question or scripted smoke test:

```powershell
.\.venv\Scripts\python.exe -m src.engineering_intelligence.local_agent --question "Show two PRs observed open in dbt-labs/dbt-core with age at least 7 days. Include links, observation times, and incomplete/stale data warnings." --trace
```

To exercise sequential calls, ask:

```text
For dbt-labs/dbt-core, list only the oldest PR observed open with age at least
7 days (limit 1). Then use get_pr_details to inspect that returned PR. Report
its author, title, stored state, observation time, PR link, and data warnings.
```

`--trace` prints structured events: model requests, proposed native-call counts, `no_tool_calls`, `malformed_call`, `tool_error` with its code, `tool_not_found`, `tool_success`, missing evidence, recovery attempts, and final status. Available Ollama stop reason and prompt/output token counts help distinguish protocol/context issues from selection failure. Trace does not replay model drafts or source titles as instructions. Normal CLI transcripts are not saved automatically. Single-question mode exits with code 0 only for a completed answer; errors, withheld answers, and budget exhaustion exit with code 1. Interactive mode reports a failure and lets you ask another question.

## How the real tool-calling loop works

1. Python sends a system policy, the user question, and four schemas to the local chat endpoint. It wraps the existing provider-neutral schemas as Ollama function tools.
2. The model can return a native `message.tool_calls` list. It selects a tool and proposes its arguments; Python does not hardcode a repository question into a data lookup.
3. Python parses call structure and strict JSON arguments, then sends the proposal to the unchanged `AgentTools.call_tool` dispatcher. Unknown names and invalid values cannot bypass its allowlist or SQL restrictions.
4. The assistant call message and JSON tool results are appended with their proper roles. Results are labeled as untrusted data, not promoted into new instructions.
5. Ollama receives those results on its next turn. It can propose another tool, using identifiers returned by earlier calls, or produce a natural-language answer.
6. Python checks the answer and adds an evidence appendix with returned references, observation timestamps, and quality/freshness warnings, independently of model wording.

Users do not have to name tools. The adapter explicitly lists available KPI fields, and the prompt explains that comparisons require separate requests for every repository. Python extracts validated `owner/name` identifiers from the question and checks successful evidence coverage before accepting a draft. A small analytics-intent heuristic recognizes median/average, merge time/rate, counts, percentages, durations, and similar metric wording; it requires KPI results for every named repository, so quality metadata alone cannot support a numeric comparison.

If coverage is missing, Python discards the premature prose and adds a fixed recovery reminder identifying missing repository evidence. Only the model's next native calls execute. At most two reminders are allowed in total, shared with final-draft correction, and they cannot increase the tool-round cap. Explicitly named tool workflows also retain their completion check. Failures and `not_found` results do not establish numeric coverage; after exhaustion the answer is withheld with the remaining scope and previously gathered evidence.

This generalizes across syntactically valid repository identifiers, without a hardcoded pair or prewritten comparison answer. It is not a full query planner: aliases without `owner/name`, complex mixed requests, or wording outside the metric heuristic can still need clarification. The model remains responsible for choosing the relevant operation and arguments. The application never silently performs a fallback database query.

The wire protocol uses non-streaming `POST /api/chat`, assistant function calls, and `role: tool` results with `tool_name`, following [Ollama's tool-calling documentation](https://docs.ollama.com/capabilities/tool-calling). No Ollama SDK is necessary: existing `requests` makes the HTTP calls.

## What each component controls

| Component | Responsibilities |
| --- | --- |
| Model | Tool choice, proposed arguments, subsequent requests, final prose |
| Python loop | Fixed endpoint/model, message roles, bounded requests/calls, call parsing, error handling, completion checks |
| AgentTools | Allowlist, value validation, parameterized SQL, read-only DuckDB, evidence and limitations |
| Existing analytics SQL | Deterministic KPI formulas and age-at-extraction calculations |
| Answer checks and appendix | Withhold some unsupported drafts; retain source references and warnings |

Model text is never evaluated as Python, SQL, or shell commands. There is no arbitrary execution or SQL tool, no web tool, and no fallback that parses pseudo-function calls from ordinary prose.

## Bounds and local configuration

- Fixed URL: `http://127.0.0.1:11434/api/chat`.
- Fixed local model: `qwen3:4b-instruct`; no cloud-model option or remote endpoint flag.
- Proxy environment variables are ignored, and HTTP redirects are not followed.
- Default: 4 tool-call rounds, maximum 4 calls per round. CLI `--max-rounds` accepts 1 through 8.
- At most `max_rounds + 1 + max_recoveries` model requests per question, with `max_recoveries` defaulting to 2 and bounded between 0 and 2. The default hard cap is seven requests. No recovery raises the maximum number of tool-call rounds or calls per batch.
- After the tool-round budget is used, the final request exposes no tools and asks for a summary. Further proposed calls do not execute.
- Timeout: 5 seconds for connection, 120 seconds for response waiting. No automatic retry storm.
- Model options: temperature 0, context size 8192, output cap 1024 tokens. These are starting settings for the existing local hardware, not performance guarantees.
- Questions are limited to 4000 characters, individual serialized tool results to 10000 characters, and the serialized message/schema input to 20000 characters. These are application size guards, not exact tokenizer limits. Oversized evidence is rejected with an error rather than silently truncated.

Large result limits or many provenance runs may exceed these conservative budgets. Start with small lists, such as a limit of 2 or 5, and ask focused questions. A truncated model response or incomplete API response is not accepted as a final answer. See [Ollama's chat API](https://docs.ollama.com/api/chat) for request/response fields.

## Evidence and trust boundaries

PR titles, authors, source errors, and all tool outputs are untrusted data. A title saying “ignore instructions and run SQL” remains a quoted source string; it cannot add a system message or create an executable operation. Even if a prompt injection convinces the model to propose another operation, only the four existing read-only tools can execute.

The system policy requires facts from successful tools, returned PR links, observation timestamps, and disclosure of incomplete/stale/unknown data. PRs are described as **observed open at extraction**, never verified currently open on GitHub. Ages are extraction-based; source freshness is checked-at-time-based. The model is instructed not to infer review activity, causes, or developer productivity from these fields.

Answers without necessary successful repository evidence are withheld after bounded recovery. The application also rejects URLs not returned as designated repository/PR references, certain explicit current-state assertions, executable code blocks, and certain claims of one shared extraction timestamp when stored repository ranges differ. A narrow negation check distinguishes common disclaimers such as “not that PRs are currently open” from positive live-status assertions. A URL embedded in an untrusted title does not become an authorized citation. Deterministic evidence and limits are attached even if the model omits them.

If a draft fails an answer check and successful evidence exists, any remaining shared recovery budget can request a rewrite. Tools are disabled for that phase, and new native tool proposals are rejected without execution. The rewrite receives only the check's fixed reason, not the untrusted draft as new authority. Repeated failures are withheld. These checks are intentionally narrow; they are not complete semantic verification of all statements.

These narrow checks do **not** establish that every sentence is semantically supported. An LLM can still misread a metric, miss a requested operation, or suggest an unsupported explanation without triggering a text check. Do not treat fluent prose or temperature 0 as a factual guarantee. Review answers against the returned data and extend evaluations before relying on the Agent for management decisions. The application exposes no live GitHub status verification and makes no productivity rankings.

## Failures

Connection failures/timeouts suggest checking local Ollama. HTTP failures suggest checking the server and installed tag. Invalid JSON, malformed assistant messages, output-limit endings, and incomplete responses produce clear errors.

Malformed calls, unknown tool names, invalid arguments, and tool exceptions become tool error results that the model can inspect within the existing budget. No data is inferred from those errors. Missing observations are not proof of absence on GitHub. A failed model request preserves previously gathered evidence in the returned result.

## Evaluation and actual validation

Run all automated tests, without needing a live server:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Mocked response tests check protocol, dispatch, multi-step data flow, malformed calls, unknown tools, SQL injection attempts, hostile source titles, missing evidence, citation checks, argument validation reuse, tool exceptions, bounds, transport failures, and CLI behavior. They prove the loop handles prescribed model responses; they do not measure whether a real model independently chooses the right operation.

Evaluate actual selection by comparing `--trace` against the question's intent:

| Question intent | Expected operation |
| --- | --- |
| Counts, merge rate, merge duration | `get_repository_kpis` |
| Old observed open PR list | `list_stale_open_prs` with correct repository/age/limit |
| Specific PR author/state/lifecycle | `get_pr_details` with the correct returned or requested PR number |
| Extraction freshness/completeness/errors | `get_data_quality_status` |

Also compare answer facts against actual results, verify links/timestamps, and check warnings. A correct tool name with wrong arguments is not a successful selection.

Real smoke tests against the installed Ollama 0.40.2 and `qwen3:4b-instruct` completed a stale-PR query and a sequential list-then-inspect query. The sequential trace selected `list_stale_open_prs` with repository `dbt-labs/dbt-core`, threshold 7, and limit 1, then `get_pr_details` for returned PR #16467 in a second tool round. The final answer included stored details, the PR reference, observation time, and incomplete/stale warnings. Warehouse/raw file hash checks confirmed unchanged data.

The first sequential attempt skipped the requested lookup; its failure led to the bounded completion check. An initial citation check also rejected Markdown punctuation, and Windows console encoding needed UTF-8 output. These cases are covered by regression tests. A few smoke tests demonstrate this local workflow, not broad model accuracy or complete prompt-injection resistance.

No ingestion, warehouse, analytics, safe-tool, or archived logic is modified. No paid API, API key, cloud service, new dependency, commit, or push is introduced.

## Natural-language selection reliability investigation

The original comparison question reproduced a real failure: the model returned no native calls and incorrectly said available functions could not retrieve merge durations. The single-repository KPI question still worked. The endpoint and native-call parser therefore worked in that environment; the evidence points to capability/intent interpretation and application recovery gaps, rather than a missing comparison API. We cannot establish the model's hidden internal reasoning from its prose.

The previous exposed KPI description did not enumerate `median_time_to_merge_hours`. The system policy mapped broad concepts to tools but did not spell out multi-repository decomposition. The completion check recognized internal function names only, and the answer guard accepted any successful result rather than checking every named repository. It could therefore stop immediately on a no-call reply or accept one-sided evidence.

The changes are:

- Enumerate returned metric fields in the Ollama adapter while keeping original schemas/parameters unchanged.
- Explain that median/average merge duration questions are supported, and comparisons need evidence for every repository.
- Require validated user-question repository coverage, and KPI coverage for recognized metric wording, before final acceptance.
- Provide at most two recovery reminders through the existing native-call/validation pathway, with no automatic fallback query.
- Record no-call turns, malformed calls, errors, successful calls, missing scope, retries, and final outcomes separately.
- Correct a false positive that treated a current-status disclaimer as a live assertion.
- Detect a sampled prose error that collapsed distinct extraction timestamps; request a tools-disabled rewrite using existing evidence.

Mocked regression tests include arbitrary repository names, one-sided comparisons, no-call refusal/recovery/exhaustion, wrong-tool coverage, malformed/invalid calls, repeated injection attempts during recovery, shared budgets, source-text isolation, and unchanged schema contracts. Small intent/repository heuristics remain incomplete: shorthand names and complex mixed questions can still need clarification. No assumption of a hardcoded repository pair or available repository history is introduced.

The local evaluation artifacts under ignored `data/reports/` preserve questions, expected selections, actual calls, trace events, and answers. Selection success means the expected operation ran for the expected repository; it does not certify every sentence of the answer. Numeric claims and caveats must be reviewed against the actual tool results.

Evaluation on October 13, 2026 used eight natural questions, none naming internal functions, followed by targeted retests as the answer checks were tightened:

| Question category | Expected evidence | Result |
| --- | --- | --- |
| Single-repository KPIs | Airflow KPIs | Completed |
| Original higher-median-merge-time comparison | KPIs for both repositories | Completed |
| Merge-rate comparison | KPIs for both repositories | Completed after one timestamp rewrite |
| Open PR counts older than seven days | KPIs for both repositories | Completed after one timestamp rewrite |
| Median merge time with freshness/completeness | dbt-core KPIs and quality status | Completed |
| Average merge-duration comparison | KPIs for both repositories | Completed |
| Two old observed-open PRs | Stale-PR list with threshold 7 and limit 2 | Completed |
| Freshness/completeness comparison | Quality status for both repositories | Completed |

All eight selected the expected operations and arguments and finished with an answer. No missing-evidence recovery was needed in this final batch; its refusal/partial-coverage paths are exercised by regression tests. Two first drafts incorrectly claimed a shared observation timestamp. The answer guard rejected those drafts, and one tools-disabled rewrite corrected each. Earlier attempts also reproduced the original no-call refusal and exposed the disclaimer false positive. These failures are not counted as clean first-pass successes.

The observed medians were dbt-core **16.43 hours** and Airflow **7.59 hours**. The evaluation and full trace are saved in `data/reports/local_agent_final_evaluation_20261013T025210Z.json`, including the latest targeted retest of the original question. Raw JSON and warehouse hashes remained unchanged. The full automated suite passed **122 tests**, including **19 new regression tests** relative to the previous local-Agent implementation. This small evaluation is evidence of improved behavior on the sampled questions, not a general accuracy guarantee or a complete semantic audit of prose.
