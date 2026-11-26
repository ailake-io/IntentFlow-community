# AI to suggest fixes (LLM) and decisions with Jev

[Português](pt-BR/AI.md)

AI suggestions ship in the core package (formerly Enterprise); no license is needed. Install: [INSTALLATION](INSTALLATION.md).

Two different roles, on purpose:

| | **LLM** (Anthropic, OpenAI, Gemini, local model) | **Jev** (TypeSafe, a decision model) |
|---|---|---|
| Does | **Writes** the SQL edit | **Decides**: returns a choice/score/yes-no with a probability, never text |
| When | Only when the fix needs reasoning | Finding triage, routing and the risk gate |
| Cost/latency | Seconds to minutes, expensive | Fast and cheap (vendor claim; not measured here) |
| Who is in charge | A person: approves and validates | Python code reads the number and decides the policy |

Nothing is applied without **human approval + sandbox validation**; the AI fix goes through the same funnel as rule-based fixes
(propose → approve → validate → apply → roll back), with the diff in view. The validation is bound to the hash of the edit; a policy that requires
`build` does not accept `parse`. Extra review requires an approver other than the author, even with a single registered user.
Validation uses Docker without network, a minimal environment, a temporary DuckDB profile and resource limits: [VALIDATION_RECOVERY](VALIDATION_RECOVERY.md).

## Turning it on (UI: Settings → "AI to suggest fixes")

| Provider | Key (environment variable) | Model | Base URL |
|---|---|---|---|
| Anthropic (Claude) | `ANTHROPIC_API_KEY` | your choice (no default) | `https://api.anthropic.com` |
| OpenAI | `OPENAI_API_KEY` | your choice (no default) | `https://api.openai.com/v1` |
| Google Gemini | `GEMINI_API_KEY` | your choice (no default) | `https://generativelanguage.googleapis.com/v1beta` |
| Local model (Ollama, LM Studio, vLLM, llama.cpp, Docker Model Runner) | optional | whatever you have | `http://127.0.0.1:11434/v1` (Ollama) |

- **Key:** a server environment variable (recommended) **or** the field on screen, which stores it **encrypted** in the store (requires
  `INTENTFLOW_SECRET_KEY`, or a SQLite file with its key file). The key is **never** returned by the API nor written to the audit trail.
- **Audit:** each call records `llm_call` with provider, model, token counts and duration in the audit trail; prompts, answers and keys are never logged.
- **Model:** there is no default; nothing is called until a model is set. **List models** (or `intentflow llm models`) asks the provider which models the account can use.
- **List models / Test connection:** buttons on the same screen (they use the typed values, before saving).
- **CLI:** `intentflow llm set --provider local --model qwen2.5:3b --base-url http://127.0.0.1:11434/v1`, `intentflow llm set-key`
  (asks for the key without echoing it), `intentflow llm test`, `intentflow llm models`, `intentflow fix-ai <finding-id>`.
- **Language:** the model writes its explanation in the language of the request (English or Portuguese).

The model names above are code defaults, not evidence of current provider/account availability. Paid endpoints were not exercised with real keys; list models and test the connection with your account. The Ollama results below belong to an earlier run. Recent evidence: [VALIDATION_STATUS](VALIDATION_STATUS.md).

### Jev
Settings → "Decisions with Jev". Two access paths, per Jev's public documentation:

| Access | Key | Call |
|---|---|---|
| `openrouter` | `OPENROUTER_API_KEY` | `POST https://openrouter.ai/api/alpha/decisions`, model `typesafe/jev-1.13` |
| `typesafe` | `TYPESAFE_API_KEY` | `typesafe-sdk` (`pip install typesafe-sdk`), `client.system_one(state, questions)` |

Questions IntentFlow asks Jev about each finding: **is it a real problem?** (yes/no), **is it probably a false positive?** (yes/no),
**urgency** (can wait → today), **fix path** (mechanical rename, expand `SELECT *`, LLM edit, metadata/test, investigate data, no action). About each
generated edit: **does it change semantics?**, **risk** (low/medium/high), **does it touch unrelated logic?**. **Triage** button in Findings.

What the code does with the number (deterministic, `jev.policy_for_risk`): risk ≥ 1.2 **or** changes semantics ≥ 70% **or** unrelated logic ≥ 50%
⇒ `build` validation and a highlighted "extra review". Jev never approves, applies or silences anything.

**"Suggest fix" flow** (REFLEX): with Jev on, if the indicated path is mechanical and a rule-based fix exists, **the LLM is not even called**;
if Jev says "no action"/"investigate data" with ≥ 80% confidence, IntentFlow says so and only goes on with "force".

## Privacy: what leaves the server
The affected model's SQL (up to 3 files, 14 thousand characters each), column names and types of related models, downstream model names, the
finding text and its evidence. The `sample`, `rows`, `observed` and `examples` fields are removed at the top level; that is **not anonymization**:
nested evidence, free text and SQL literals may still hold sensitive data. A local endpoint avoids sending to a remote provider only if the model
server also runs locally without external forwarding. Review the content and your company's policy before using an external provider.

## How an edit is accepted (safety)
The model returns `edits` (`file`, `find`, `replace`), not the whole file. Each `find` must appear **exactly once**; only the finding's files are
accepted; at most 8 edits; files truncated when sent are not edited. Deterministic guards: **reject** removing `ref()`/`source()` (it would break
lineage) and **reject** adding `env_var()`, `run_query()`, `pre_hook`/`post_hook`, `on-run-*` or `{% do/call/statement %}` (an AI edit never introduces them); **warn and require extra review** when removing Jinja/macros or changing more than 24 lines. Non-JSON output, ambiguous or out-of-scope
edits become a clear error; nothing is created. The user's write scope (per-folder/model patterns) is checked against every file the proposal changes, when it is
created and again on every approve/validate/apply/rollback.

## What was observed with a real local model (qwen2.5:3b, Ollama)
The pipeline worked end to end (≈ 67 s). The quality was **not** good: for a `SELECT *` the model replaced three macro calls (`cents_to_dollars`)
with `cast(... as decimal)`, unrelated to the finding, made up an explanation and declared 95% confidence. Hence: (1) the model's "confidence"
shows up as *self-reported, not calibrated*; (2) removing macros trips the guard and the extra review; (3) small models are good for testing the
flow, **not** for trusting. Use strong models and read the diff.

## Limits
- **Jev:** verified only against a simulated server with the documented format; the API is in early access and may change.
- **Anthropic, OpenAI, Gemini:** request/response format verified against a local server; never against the real services with a real key.
- **Local:** verified against a real Ollama (list models, chat, full pipeline).
- Only edits model `.sql` files; YAML fixes (description, tests) not yet. Owner (DBT003) and `unique_key` (DBT002) have their own rule, without an LLM.
- In the UI the suggestion runs in the background (`/ai-status`); with a local model the default limit is 600 s.
- No cost/spend limit per call; use the provider's limits.
