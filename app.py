import streamlit as st

st.set_page_config(
    page_title="Trader Custom Tools", layout="wide", initial_sidebar_state="expanded"
)

st.title("Trader Custom Tools")

st.markdown(
    """
Welcome! This is a collection of in-house tools built for the trading desk.

Pick a tool from the **sidebar on the left**, or use the shortcuts below. Every tool
opens in this same window, and you can always come back here by clicking **Homepage**
in the sidebar.
"""
)

st.divider()

st.header("Available Tools")

st.subheader("📈 Regression Tool")
st.markdown(
    """
Builds a statistical model that explains how a generator's output (or MISO day-ahead
congestion) moves with weather and grid conditions, then uses that model to produce a
forward-looking hourly forecast.

**What you do:** connect to the database, pick a data vendor and a generator, choose
which wind / load / solar / temperature variables to include, set your date range, and
run it.

**What you get back:** accuracy scores telling you how well the model fit history,
charts of predicted vs. actual, a ranking of which inputs mattered most, and an hourly
forecast you can eyeball or export.

**Good for:** sanity-checking a trade thesis, seeing which weather regions actually
drive a plant, and getting a quick second opinion on a forecast.
"""
)
st.info("Open it from **regression tool** in the sidebar on the left.", icon="📈")

st.divider()

st.header("Adding a New Tool to This Site")

st.markdown(
    """
Every tool on this site is just one Python file living in the `pages/` folder. Drop a
new file in there and it shows up in the sidebar automatically — there is no master list
to update and nothing to register.

### The three steps

**1. Create the file.** Add your script to the `pages/` folder and give it a lowercase
name with underscores instead of spaces, for example `pages/spark_spread_tool.py`.
Streamlit turns that filename into the sidebar label, so `spark_spread_tool.py` shows up
as *spark spread tool*.

**2. Give the page a title.** At the top of your new file, add these lines so the page
looks like the rest of the site:

```python
import streamlit as st

st.set_page_config(page_title="Spark Spread Tool", layout="wide")
st.title("Spark Spread Tool")
```

**3. Add it to this homepage.** Copy the Regression Tool block above, change the
heading, and rewrite the description.

That last step is optional — the sidebar link appears either way — but it means the next
person who opens this site can tell what your tool actually does.

Do **not** use `st.page_link()` to link to your page. It crashes with
`KeyError: 'url_pathname'` in apps that use the `pages/` folder, because Streamlit only
fills that field in for the newer `st.navigation()` routing API. Describe the tool and
let the sidebar do the linking.

### A few things worth knowing

- **Ordering the sidebar.** Streamlit sorts pages alphabetically by filename. If you
  want a specific order, put a number and an underscore in front: `1_regression_tool.py`,
  `2_spark_spread_tool.py`. The numbers are hidden from the sidebar label.
- **New Python packages.** If your tool needs a library that isn't already installed,
  add its name on its own line in `requirements.txt`. The deploy script installs
  everything in that file.
- **Database access.** Never put credentials in your script. The shared `lib/db.py`
  handles all of it — two lines and you have a connection:

  ```python
  from lib import db

  if not db.gate():          # shows connection status in the sidebar
      st.stop()
  df = pd.read_sql("SELECT ...", db.engine())
  ```

  On the server the credentials come from `config/mysql.yml` and nobody is prompted for
  anything. Running locally, `db.gate()` asks for the password once per session — if your
  local database account has no password, leave the box empty and press **Connect**. Set
  `TIOS_DB_PASSWORD` in your shell to skip the prompt entirely.
- **Plant and region lists.** If your tool needs the generator or forecast-region
  dropdowns, use `from lib.metadata import load_metadata` rather than re-querying — the
  lists are already loaded and cached for the whole app.
- **Shared code goes in `lib/`, not `pages/`.** Streamlit turns every file in `pages/`
  into a page, so a helper module dropped there shows up as a blank entry in the sidebar.
- **Shared work stays shared.** Anything you save in `st.session_state` is visible to
  every page in your browser session, which is handy for passing a loaded dataset from
  one tool to another.

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
