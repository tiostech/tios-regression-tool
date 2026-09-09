import streamlit as st

st.set_page_config(
    page_title="Trader Custom Tools", layout="wide", initial_sidebar_state="expanded"
)

st.title("Trader Custom Tools")

st.markdown(
    """
Welcome! This is a collection of in-house tools built for the trading desk.

Pick a tool from the **sidebar on the left**. Every tool opens in this same window, and
you can always come back here by clicking **Homepage** in the sidebar.

If a tool is broken, the numbers look wrong, or anything below doesn't work the way it's
written here, **contact someone from the tech team** — don't work around it.
"""
)

st.divider()

st.header("Getting the App Running on Your Laptop")

st.markdown(
    """
You only need this if you want to change something. To *use* the tools, just open the
live site — nothing to install.

Everything below happens in **Terminal**. Git and your GitHub access are already set up
for you, so you can start at step 1.

### First time only

**1. Download the project.**

```bash
git clone git@github.com:tiostech/tios-regression-tool.git
cd tios-regression-tool
```

The `cd` puts you inside the project folder. Every command on this page assumes you are
in there.

**2. Create a virtual environment.** This is a private folder of Python packages that
belongs to this project alone, so installing something here can't break anything else on
your machine:

```bash
python3 -m venv .venv
```

You only ever run this once. It creates a hidden `.venv` folder that is deliberately
excluded from Git — it never gets committed or shared.

**3. Turn the virtual environment on:**

```bash
source .venv/bin/activate
```

Your Terminal prompt now starts with `(.venv)`. That's how you know it's on. It switches
off when you close the Terminal window, so you'll run this line again each time you come
back — that's normal, not a mistake.

**4. Install the packages the app needs:**

```bash
pip install -r requirements.txt
```

This reads `requirements.txt` and downloads Streamlit, pandas, and everything else. It
takes a couple of minutes the first time and prints a lot of text — that's fine.

**5. Set up your database connection.** Copy the sample file and fill in your own
credentials:

```bash
cp config/mysql.yml.sample config/mysql.yml
```

Open `config/mysql.yml` in a text editor and replace `CHANGE_ME` for `username` and
`password` with your database login. If your local account has no password, use
`password: ""` (with the quotes).

`config/mysql.yml` is excluded from Git on purpose, so your credentials stay on your
laptop. **Never** put real credentials in any other file.

**6. Start the app:**

```bash
streamlit run app.py
```

It opens in your browser at `http://localhost:8501`. Press **Ctrl+C** in the Terminal to
stop it.

### Every time after that

Two lines to get going again:

```bash
source .venv/bin/activate     # turn the virtual environment on
streamlit run app.py          # start the app
```

While the app is running, Streamlit watches your files. Save a change in your editor and
the browser offers a **Rerun** button — you don't need to stop and restart.

### If something isn't working

| What you see | What to do |
| --- | --- |
| `command not found: streamlit` | The virtual environment isn't on. Run `source .venv/bin/activate` — look for `(.venv)` in your prompt. |
| `ModuleNotFoundError: No module named ...` | A package is missing. Run `pip install -r requirements.txt` with the environment on. |
| Somebody added a new package | Run `pip install -r requirements.txt` again after pulling their changes. |
| `Config file not found` in the sidebar | You skipped step 5 — copy `config/mysql.yml.sample` to `config/mysql.yml`. |
| `Cannot reach the database` in the sidebar | Your credentials in `config/mysql.yml` are wrong, or you aren't connected to the network the database sits behind. Check the login first, then ask. |
| Port 8501 is already in use | The app is already running in another Terminal window. Use that one, or run `streamlit run app.py --server.port 8502`. |

**Stuck on any of this? Contact someone from the tech team.** You are not expected to
debug your own setup — that's what they're there for. Copy the last few lines of the
Terminal output into your message, since that's the part that says what actually went
wrong.
"""
)

st.divider()

st.header("Adding a New Tool to This Site")

st.markdown(
    """
Every tool on this site is just one Python file living in the `pages/` folder. Drop a
new file in there and it shows up in the sidebar automatically — there is no master list
to update and nothing to register.

### 1. Create the file

Add your script to the `pages/` folder and give it a lowercase name with underscores
instead of spaces, for example `pages/spark_spread_tool.py`. Streamlit turns that
filename into the sidebar label, so `spark_spread_tool.py` shows up as
*spark spread tool*.

### 2. Start from this skeleton

```python
import os
import sys

import pandas as pd
import streamlit as st

# Shared helpers live in lib/ at the repo root. Streamlit puts the entrypoint's
# directory on sys.path, so this only matters if a page is ever launched
# directly instead of through app.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import db

st.set_page_config(page_title="Spark Spread Tool", layout="wide")
st.title("Spark Spread Tool")

if not db.gate():          # sidebar connection block; False until it's ready
    st.stop()

df = pd.read_sql("SELECT ...", db.engine())
st.dataframe(df)
```

### 3. Optionally link it from this homepage

```python
st.page_link("pages/spark_spread_tool.py", label="Open the Spark Spread Tool", icon="⚡")
```

The sidebar link appears either way, but a short blurb here tells the next person what
your tool actually does.

### Database access

Never put credentials in your script — `lib/db.py` handles all of it.

- `db.gate()` renders the sidebar connection block and returns `True` once the app can
  query. Call `st.stop()` when it returns `False`. Pass a label
  (`db.gate("1. Database")`) if you want a different sidebar heading.
- `db.engine()` returns the shared SQLAlchemy engine — hand it to `pd.read_sql()` or
  use `engine().connect()` directly. It is cached per credential set, so don't build
  your own.
- `db.reset()` drops the cached engine and every cached query result. `db.gate()`
  already exposes this as the **Reload data** button.

On the server, credentials come from `config/mysql.yml` and nobody is prompted for
anything. Running locally, `db.gate()` asks for the password once per session — if your
local database account has no password, leave the box empty and press **Connect**. Set
`TIOS_DB_PASSWORD` in your shell to skip the prompt entirely.

### Shared reference data

`lib/metadata.py` already loads and caches the generator and forecast-region lists used
by the dropdowns:

```python
from lib.metadata import load_metadata

meta = load_metadata()     # cached for the whole app, not per page
```

Use it rather than re-querying those tables yourself.

### A few things worth knowing

- **Ordering the sidebar.** Streamlit sorts pages alphabetically by filename. If you
  want a specific order, put a number and an underscore in front:
  `1_first_tool.py`, `2_second_tool.py`. The numbers are hidden from the sidebar label.
- **Shared code goes in `lib/`, not `pages/`.** Streamlit turns every file in `pages/`
  into a page, so a helper module dropped there shows up as a blank entry in the sidebar.
- **Cache expensive work.** Use `@st.cache_data` for query results and computed frames,
  `@st.cache_resource` for connections and other long-lived objects. Give slow ones a
  `ttl` so they refresh eventually.
- **Shared state stays shared.** Anything you save in `st.session_state` is visible to
  every page in your browser session, which is handy for passing a loaded dataset from
  one tool to another. Namespace your keys so pages don't collide.
- **New Python packages.** If your tool needs a library that isn't already installed,
  add its name on its own line in `requirements.txt`. The deploy script installs
  everything in that file.

### Testing your tool before it goes live

From the project folder, run:

```bash
source .venv/bin/activate
streamlit run app.py
```

That opens the site in your browser using your local copy of the code. Your new page
will be in the sidebar. Nobody else can see it yet — it is running off the files on your
own laptop. When you're happy with it, follow the Git steps below to get it reviewed and
merged, then run `bin/deploy` to push it to the live server.
"""
)

st.divider()

st.header("Using Git: Saving and Sharing Your Changes")

st.markdown(
    """
Git is the system that keeps track of every change made to this project, and GitHub is
the website where those changes are stored and reviewed. You do not need to understand
how it works underneath — the commands below are the whole job, in order.

**The short version:** never edit `master` directly. Make your own copy (a *branch*),
change whatever you like there, then ask someone to look it over before it becomes
official.

A few words that show up constantly:

| Word | What it actually means |
| --- | --- |
| **repository** (repo) | The project folder, plus its entire history. |
| **master** | The official version. This is what gets deployed to the live server. |
| **branch** | Your personal copy of `master` to work on, so nothing you do can break the official version. |
| **commit** | A save point, with a note describing what you changed. |
| **push** | Upload your commits from your laptop to GitHub. |
| **pull** | Download everyone else's commits from GitHub to your laptop. |
| **pull request** (PR) | A request to fold your branch into `master`, with a page where a colleague can review it first. |

### The everyday workflow

Every command below assumes you are inside the project folder — see
**Getting the App Running on Your Laptop** above if you haven't downloaded it yet.

**Step 1 — Start from an up-to-date copy of `master`.** Do this every single time you
begin something new, so you aren't building on stale code:

```bash
git checkout master
git pull
```

**Step 2 — Make a branch for your work.** Name it after what you're doing, lowercase
with dashes:

```bash
git checkout -b spark-spread-tool
```

You are now working on your own branch. Nothing you do here affects `master` or anyone
else until you say so.

**Step 3 — Do your work.** Edit files, add pages, test locally with
`streamlit run app.py`. Take as long as you need.

**Step 4 — Save your work with a commit.** Check what you changed, then save it:

```bash
git status                                  # lists the files you touched
git add -A                                  # stage everything you changed
git commit -m "Add spark spread tool"       # save it, with a note
```

Commit whenever you finish a meaningful chunk — several small commits are better than
one giant one. Write the note as an instruction: "Add spark spread tool", "Fix date
filter on outage page".

**Step 5 — Push your branch to GitHub:**

```bash
git push -u origin spark-spread-tool
```

The `-u origin spark-spread-tool` part is only needed the first time you push a new
branch. After that, plain `git push` is enough.

**Step 6 — Open a pull request.** After pushing, Git prints a link in the Terminal —
click it. (Or go to
[the repository on GitHub](https://github.com/tiostech/tios-regression-tool) and click
the **Compare & pull request** button that appears at the top.)

Give it a clear title, write a couple of sentences in the description saying what the
change does and how you tested it, then click **Create pull request**. On the right-hand
side, under **Reviewers**, pick a colleague.

**Step 7 — Get it reviewed.** Your reviewer may leave comments asking for changes. To
address them, just keep working on the same branch:

```bash
git add -A
git commit -m "Address review comments"
git push
```

The pull request updates itself automatically — you do not open a new one. Reply to the
comments on GitHub so your reviewer knows to take another look.

**Step 8 — Squash and merge.** Once your reviewer approves, click the arrow next to the
green merge button and choose **Squash and merge**, then confirm. This bundles all of
your commits into a single tidy entry on `master`. Click **Delete branch** afterwards —
it's already saved in the history, and it keeps the branch list clean.

**Step 9 — Go back to `master` and deploy.** Bring your laptop up to date with the
change you just merged, then push it live:

```bash
git checkout master
git pull
bin/deploy
```

### Rules of thumb

- **Never commit directly to `master`.** Always work on a branch and go through a pull
  request, even for a one-line fix.
- **One branch per task.** Don't bundle an unrelated fix into a branch about something
  else — it makes review harder and it's harder to undo later.
- **Keep pull requests small.** A reviewer can give a genuinely useful opinion on 100
  lines. On 2,000, they will just say "looks good".
- **Never commit passwords or credentials.** `config/mysql.yml` is deliberately excluded
  from Git for this reason. If you think you committed a secret, say something
  immediately rather than trying to quietly delete it.
- **Pull before you start, not just when you're stuck.** Most conflicts come from
  branching off a copy of `master` that was already a week old.

### When something goes wrong

| Situation | What to do |
| --- | --- |
| "Which branch am I on?" | `git branch --show-current` |
| "What have I changed?" | `git status` (files) or `git diff` (the actual lines) |
| "I want to throw away my changes to one file." | `git checkout -- path/to/file.py` — this cannot be undone. |
| "I committed to `master` by mistake." | Stop pushing. Run `git branch my-fix` then `git reset --hard origin/master`. Your work is safe on `my-fix`. |
| "Git says I have a merge conflict." | Two people edited the same lines. Ask for help the first time — it's much easier shown once than read about. |
| "`bin/deploy` says my repo isn't clean." | You have uncommitted changes or you're not on an up-to-date `master`. Run `git status`, then `git checkout master && git pull`. |

**When in doubt, stop and contact someone from the tech team.** Almost nothing in Git is
truly unrecoverable, but the fix is far simpler before you push — and simpler still
before you try to untangle it yourself. Nobody minds being asked; everybody minds
untangling a repo afterwards.
"""
)
