# CTS DPS model inventory (for the model risk team)

**Decision (17 Nov 2026): DPS uses the architect's Document Agent on the bank's Azure OpenAI deployments (GPT).**
Rules always read first. The agent proposes values with page and quote; rules, a second model, Java business rules
and a person decide. Only the pages a job needs are sent, only to the bank's own Azure endpoint.

| Component | Used for | Runs where | Sends data out? | Owner |
| --- | --- | --- | --- | --- |
| Document Agent, route `strong` (e.g. GPT-4.1) | Reads the selected pages with the job's skill and tools; proposes values with page and quote | Bank's Azure OpenAI deployment | Selected page text, to the bank's Azure endpoint only | DPS lead |
| Route `small` (e.g. GPT-4.1-mini) | Second opinion on the document type (rules decide first) | Bank's Azure OpenAI deployment | First two pages | DPS lead |
| Route `vision` (e.g. GPT-4o) | Scanned pages OCR cannot read; image-only documents' type | Bank's Azure OpenAI deployment | Up to 4 page images | DPS lead |
| RapidOCR (ONNX models, CPU) | Text from scanned pages, with a confidence per line | DPS parser container (no network) | No | DPS lead |
| Docling layout model | Difficult tables on scanned statements (HEAVY pool) | DPS parser container (no network) | No | DPS lead |
| Confidence scorer | Rule table now; logistic regression + isotonic calibration after 200 reviewed fields per type | DPS worker | No | Model owner |

**Not used:** Azure Document Intelligence, any model outside the bank's Azure tenant, CrewAI's own telemetry.

## Controls

- **Switch and endpoint:** the agent runs only with `DPS_LLM_ENABLED=true`, a model route file, and a model host
  listed in `DPS_AI_ALLOWED_HOSTS`. Any other host (for example api.openai.com) is refused at start-up
  (`tests/test_llm_policy.py`, `tests/test_document_agent.py`). `GET /dps/v1/capabilities` reports `llmEnabled`.
- **Least data:** each job's prompt holds only its selected pages (`page_selection` in the skill pack), never the
  whole file. Since 18 Nov 2026 the agent's tools (`page_text`, `search_text`, `find_definition`) may open other pages
  of the same document, within the pack's tool-call limit (8 calls, at most 12,000 characters each), so it can follow
  definitions and schedule references; tool results go through the same masking. Personal data (PAN, Aadhaar, e-mail, phone, account numbers) is masked before a call. Prompts are not stored:
  the call log keeps model, prompt hash, tokens, time and outcome (`DPS_AI_TRACE_TEXT=false`, the default).
- **Grounding:** every agent value must quote text found on the page it names (or the page before or after, or
  across a page break); otherwise it is repaired once, and then only the grounded items are kept (`AI_UNGROUNDED`
  says how many were dropped). Values read from page images cannot be grounded, so they always go to a person.
- **Covenant names:** every covenant the agent proposes gets a covenant-library metric key or a code from the
  bank's covenant taxonomy (or OTHER, with the name as written kept for the reviewer); loan mechanics (drawdown,
  prepayment, default, cure) are left to the schedule skill; one item per key and clause area
  (`skills/covenants/names.py`). Since 18 Nov 2026 the agent reads covenants in four focused parts, each prompt
  listing only that part's taxonomy codes (`skills/covenants/parts.py`); events of default come back as their own
  fields, always for a person; values the document does not state and the taxonomy fills (operator, unit, test
  frequency) are flagged `TAXONOMY_DEFAULT` and go to a person (`skills/covenants/codes.py`).
- **Covenant pages and coverage:** the agent reads the covenant sections, duty pages and definitions, not only the
  pages the rules used (`skills/covenants/pages.py`). Every duty sentence on those pages must be explained by an
  extracted field or is listed for the reviewer (`document.coverage`, `COVERAGE_GAP`; `skills/covenants/coverage.py`).
- **Vote:** rules and agent agree → higher confidence; disagree → a person sees both; agent-only
  values on high-risk fields and every agent-only covenant → a person. There is no second-model re-read (removed 18 Nov 2026).
- **Numbers:** the LLM never decides compliance or calculates; formulas are validated by the Spring app (Java).
- **Prompt injection:** document text is marked as data, not instructions; suspicious text is flagged.
- **Outage and cost:** retries, circuit breaker and rate limits per route; per-job token budget and monthly
  budget. If the model is down or over budget the job is read by rules only and sent to review; it never fails.
- **Telemetry:** CrewAI telemetry and tracing are off in code and in the image (`CREWAI_DISABLE_TELEMETRY`).
- The parser containers have no network, so OCR and layout models cannot send data anywhere.
- Prompts, skills, model routes and scorer changes go through the golden-set gate and a 2-week shadow run.

## Monthly review (after go-live)

Override rate per field, agent vs rules agreement, sampled error rate (`GET /dps/v1/ops/quality`), calibration
error, tokens and cost per job type (`GET /dps/v1/ops/usage`), model failure rate. Decisions are recorded here.
