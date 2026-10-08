# CLAUDE.md

Guidance for AI assistants working in this repo.

## Who you're working with

Most people editing this repo are **traders, not software engineers**. They know the
markets and the data well. They don't necessarily know Python, Git, or Streamlit. Work
accordingly:

- Explain what you're about to change, and why, in plain language before doing it.
  When you're done, explain what changed and what to check in the app.
- Don't assume they can review a diff. Describe the effect ("the date filter now
  includes the end date") rather than the mechanics ("changed `<` to `<=`").
- Ask before anything large: refactors, renaming files, touching more than one page,
  new dependencies, or changes to `lib/` or `bin/`.
- If a request looks risky or you aren't sure it's correct, say so plainly. Suggest
  they check with the tech team rather than pressing on.

## What this repo is

A multi-page Streamlit app of trading-desk tools that read from the
`tioscore_production` MySQL read replica.

- `app.py` is the homepage. It also has the trader-facing setup, "add a tool", and Git
  instructions. Keep it accurate if you change any of those workflows.
- `pages/` has one file per tool, and each one shows up in the sidebar automatically.
  Only put pages here.
- `lib/db.py` handles all database access. `lib/metadata.py` provides the cached
  plant and region lists.
- `README.md` holds the developer setup and deployment details.

Python is **3.10** (`.python-version`). Don't use syntax or library features newer
than that.

## Numbers are the product

People make trading decisions from these tools. A wrong number that looks plausible is
worse than a crash.

- **Never change a calculation, filter, unit, time zone, or date range as a side
  effect.** If a change affects any number on screen, say so explicitly, even if it
  was requested.
- Hardcoded trading inputs must only change on explicit request, and you must call out
  the change. Examples are `MISO_CUSTOM_CONSTRAINTS` in `pages/regression_tool.py`,
  constraint IDs, region lists, and hour ranges.
- Be careful with `dt`/`hr` handling (hour-ending vs hour-beginning, DST days), `BETWEEN`
  inclusivity, joins that can duplicate rows, and aggregations that silently drop NULLs.
- Don't silently swallow errors in data loading. If a query fails, the user should see
  a warning, not an empty chart they might mistake for "no data".
- When you change logic, suggest a concrete check, such as "load ERCOT for last week
  before and after and compare the totals".

## Database rules

- **Read-only.** Never write `INSERT`, `UPDATE`, `DELETE`, `ALTER`, `DROP`, or any other
  statement that changes data or schema.
- **Always go through `lib/db.py`.** Every page starts with
  `if not db.gate(): st.stop()` and queries with `db.engine()`. Never call
  `create_engine` yourself, and never hardcode hosts, usernames, or passwords.
- **Never open, print, or commit `config/mysql.yml`**, and don't put credentials
  anywhere else. Only ever look at `config/mysql.yml.sample`.
- **Bind user input as parameters.** Use `sqlalchemy.text()` with `:name` placeholders,
  as `pages/outage_and_shadow_search.py` does. Don't paste user-entered values into
  f-strings. Table names built from fixed lists (e.g. `f"{iso}_..."` with `iso` from
  `ISOS`) are fine.
- **Keep queries bounded.** Filter by date on the large hourly tables. Don't
  `SELECT *` from a whole table.
- Tables are per ISO (`{iso}_meteologica_wind_forecasts`, `{iso}_totalgen_on_outages`,
  etc.). Not every ISO has every table, so handle a missing table gracefully.
- Use `load_metadata()` for plant and region lists instead of re-querying them, and
  cache expensive queries with `@st.cache_data(ttl=...)`.

## Making changes

- **Keep diffs small and focused.** Don't reformat, re-indent, or "clean up" code you
  weren't asked to touch. It buries the real change and makes review hard.
- Shared helpers go in `lib/`, never `pages/`.
- Avoid new packages. If one is truly needed, add it to `requirements.txt` and tell
  the user to run `pip install -r requirements.txt`.
- Match the existing style of the file you're editing.

## Checking your work

There is no automated test suite. Before saying something works:

1. Run `python -m py_compile <file>` on every file you changed.
2. Ask the user to run `streamlit run app.py`, open the affected page, and try the
   specific thing you changed. Tell them exactly what they should see.
3. Be honest about what you verified and what you didn't. "I haven't run this against
   the database" is a useful thing to say.

## Git and deployment

- **Never commit to `master`.** Create a branch for the task first
  (`git checkout -b short-description`).
- Commit with clear messages, push the branch, and open a pull request for a colleague
  to review. Never force-push and never merge your own PR.
- Never stage `config/mysql.yml`, `.venv`, or other secrets or local files. Check
  `git status` before committing.
- **Never run `bin/deploy`.** It pushes to the live production servers. A person runs
  it after the PR is reviewed and merged.
- If Git gets into a confusing state (conflicts, detached HEAD, accidental commit on
  `master`), stop and explain the situation. Recommend contacting the tech team rather
  than running `reset --hard` or other destructive commands.
