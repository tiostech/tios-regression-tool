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
streamlit run app.py
```

That opens the site in your browser using your local copy of the code. Your new page
will be in the sidebar. When you're happy with it, commit your changes to `master` and
run `bin/deploy` to push it to the live server.
"""
)
