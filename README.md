# Library Text-to-SQL (LangGraph + Ollama + SQL Server)

Natural-language (English / Hinglish / Hindi / voice-ASR text) questions -> validated T-SQL -> accurate answer (+ chart spec).

## Graph
```
correct_asr_errors -> resolve_followup -> question_understanding
   ├─ (greeting / out-of-scope / needs clarification) -> update_memory -> END
   └─ generate_sql -> execute_sql ─┬─ error/0-rows & retries left -> retry_generate -> execute_sql
                                   ├─ fatal error ------------------> END (friendly message)
                                   └─ ok -> generate_chart -> generate_answer -> update_memory -> END
```
| Node | What it does |
|---|---|
| correct_asr_errors | detects language (en / hinglish / hi), fixes near-miss domain words (`accesion`->`accession`), LLM rewrites Hindi/Hinglish/ASR text into clean English and transliterates names |
| resolve_followup | uses last turns of `thread_id` to turn "and their emails?" into a standalone question |
| question_understanding | intent (data / greeting / out-of-scope / clarify), table selection (keyword+synonym scoring + business-rule boosts + LLM + join-path bridging), builds compact schema context, matches business rules |
| generate_sql | T-SQL generation with: schema subset, real enum values (`values:`), join hints (incl. composite-PK InstId), business rules, 4 closest few-shot examples |
| execute_sql | **guard first** (read-only, allow-listed tables, real columns, no ambiguity, no PII/`SELECT *`, TOP cap, member row-level security), then runs with timeout |
| retry_generate | feeds the exact error (or "0 rows, relax filters") back to the LLM, max `MAX_SQL_RETRIES` |
| generate_chart | deterministic chart spec (bar/line/pie) when result is category+metric or user asks for a chart (`CHART_PNG=1` adds a PNG) |
| generate_answer | LLM writes a 1-3 sentence summary in the user's language/script; **numbers are verified against the data** (falls back to a deterministic summary); table is rendered by code, never by the LLM |
| update_memory | stores the turn (short-term per `thread_id`, long-term per `user_id`) |

## Why answers stay accurate
1. Live `INFORMATION_SCHEMA` decides which tables/columns exist (Excel = descriptions/synonyms only) - drift is logged at startup.
2. Enum columns (`CircIssueTransaction.status`, `CheckStatus`, `isPaid`, ...) are profiled from the DB, so filters use the *real* literals (e.g. `'Recieved'`).
3. Guard rejects hallucinated tables/columns and the error text goes back to the LLM for repair.
4. Zero-row results trigger one relaxed-filter retry.
5. Deterministic tables + numeric check on summaries = no invented numbers.
6. `scripts/evaluate.py` + `tests/golden.json` measure accuracy against reference SQL.

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# ODBC Driver 18 for SQL Server must be installed on the machine
cp .env.example .env            # fill DB_USER / DB_PASSWORD (read-only login!)
ollama pull qwen2.5:14b
ollama create qwen2.5-14b-sql -f Modelfile   # sets num_ctx 8192 -> otherwise long prompts get silently truncated
python run.py                   # http://localhost:8000/docs
```
```bash
curl -s localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question":"overdue books dikhao","thread_id":"t1","role":"admin"}'
curl -N localhost:8000/ask/stream -H 'content-type: application/json' \
  -d '{"question":"top 10 most issued books chart","role":"admin"}'
# member (sees only own data)
curl -s localhost:8000/ask -H 'content-type: application/json' \
  -d '{"question":"mere issued books","user_id":"1234","role":"member"}'
```
Response: `answer` (markdown), `summary` (short, for TTS), `columns`, `rows`, `chart`, `sql`+`tables` (admin only), `timings`.

## Production checklist
- Create a **read-only SQL login** (`db_datareader` only). The guard is defence-in-depth, not a replacement.
- `user_id` / `role` must come from your auth (JWT), not from the request body. Set `API_KEY` and put the API behind a reverse proxy with rate limiting + TLS.
- Row-level security for members is enforced in the guard (`domain.USER_SCOPED_TABLES`). For hard guarantees also expose per-member SQL views.
- Verify the three assumptions in `domain.py` against your data: `isPaid` semantics, `CircIssueTransaction.status` literals, `Fine_calculation` usage (they are profiled automatically, check `data/value_profile.json`).
- Run `python -m scripts.evaluate` after every model / prompt / schema change; grow `tests/golden.json` with real user questions.
- Logs: every SQL (ok / rejected / db_error) is in the `audit` table of `MEMORY_DB`.
- Multi-institute DB: set `DEFAULT_INST_ID`.
