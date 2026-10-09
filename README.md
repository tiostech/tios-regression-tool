# Trader Custom Tools (tios-regression-tool)

A multi-page [Streamlit](https://streamlit.io) app of in-house tools for the trading desk.
It includes the Regression Tool (GenForecast Pro) and the Outage & Shadow Price Search.
All data comes from the `tioscore_production` MySQL read replica.

This README is for developers. The app's homepage (`app.py`) has a step-by-step version
of the same setup written for traders. It also covers adding a new page and the Git
workflow.

## Layout

```
app.py                  Homepage / entrypoint (`streamlit run app.py`)
pages/                  One file per tool; each shows up in the sidebar automatically
lib/db.py               Shared DB connection: credentials, engine, sidebar gate
lib/metadata.py         Cached plant / forecast-region lists used by the dropdowns
config/mysql.yml.sample Template for config/mysql.yml (gitignored)
config/instances.yml    Deploy targets read by bin/deploy
bin/deploy              Deploys master to the EC2 instances
requirements.txt        Python dependencies
.python-version         Python version (3.10.11)
```

Only put page files in `pages/`. Streamlit turns every `.py` file there into a sidebar
entry, so shared code belongs in `lib/`.

## Local development

### Prerequisites

- **Python 3.10** (see `.python-version`). With pyenv, run `pyenv install 3.10.11`
  first; the version is then picked up automatically inside the repo.
- **Network access to the MySQL replica.** The sample config points at
  `127.0.0.1:3309`, so you need a tunnel or port-forward to the replica on that port.
- A **MySQL account** with read access to `tioscore_production`.

### Setup

```bash
git clone git@github.com:tiostech/tios-regression-tool.git
cd tios-regression-tool

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config/mysql.yml.sample config/mysql.yml
# edit config/mysql.yml: set mysql_slave.username (and password, see below)
```

### Run

```bash
source .venv/bin/activate
streamlit run app.py
```

The app opens at http://localhost:8501. Streamlit watches the source files, so after
you save a change, click **Rerun** in the browser.

### Database credentials

`config/mysql.yml` is gitignored. Never commit it, and never put credentials anywhere
else. The file looks like this:

```yaml
mysql_slave:
  host: 127.0.0.1
  port: 3309
  database: tioscore_production
  username: your_user
  password: your_password   # optional locally, see below
```

`lib/db.py` looks for the password in three places and uses the first one it finds:

1. `mysql_slave.password` in `config/mysql.yml` (this is how the servers do it)
2. The `TIOS_DB_PASSWORD` environment variable
3. A password box in the sidebar, asked once per browser session

If you leave `password:` empty in the YAML, it counts as *not set* and the lookup moves
on to steps 2 and 3. If your account really has no password, write `password: ""`.

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| `command not found: streamlit` | The venv isn't active. Run `source .venv/bin/activate`. |
| `ModuleNotFoundError` | Run `pip install -r requirements.txt` with the venv active. Run it again after pulling changes that add packages. |
| Sidebar says `Config file not found` | Copy `config/mysql.yml.sample` to `config/mysql.yml`. |
| Sidebar says `Cannot reach the database` | Check your credentials, and check that the tunnel to port 3309 is up. |
| Dropdowns are empty or stale | Click **Reload data** in the sidebar. This clears the engine and data caches. |
| Port 8501 is in use | Run `streamlit run app.py --server.port 8502`. |

## Adding a tool

Create a new file in `pages/` and start it with the DB gate:

```python
import pandas as pd
import streamlit as st
from lib import db

st.set_page_config(page_title="My Tool", layout="wide")
st.title("My Tool")

if not db.gate():
    st.stop()

df = pd.read_sql("SELECT ...", db.engine())
```

Use `db.engine()` instead of building your own engine; it is cached and shared. Use
`lib.metadata.load_metadata()` for the plant and region lists. If you need a new
package, add it to `requirements.txt` (the deploy installs it on the servers). The
homepage covers this in more detail.

## Deployment

The app runs on EC2 instances under a systemd service. `bin/deploy` updates each
instance in place.

### What `bin/deploy` does

1. Asks for confirmation. Type `yes` to continue.
2. Checks that your local repo is clean and up to date with its upstream. Unless you set
   `ALLOW_NON_MASTER=1`, you must be on `master`.
3. Looks up the targets with the AWS CLI. In `us-east-1`, it finds running instances
   where `tag:Name` matches `regressiontool-*` and `tag:env` equals `$DEPLOY_ENV`
   (default `production`). These values come from `config/instances.yml`.
4. SSHes into each instance in parallel as `tiostech@<private-ip>` and runs:
   ```bash
   cd /ebsmount/www/tios-regression-tool   # deploy_dir in config/instances.yml
   git fetch --all && git checkout <branch> && git pull
   .venv/bin/pip install --upgrade pip
   .venv/bin/pip install -r requirements.txt
   sudo systemctl restart regression-tool
   ```

The servers pull from GitHub. Your local files are never copied, so whatever you deploy
must already be pushed.

### Prerequisites for deploying

- **Ruby.** The system Ruby on macOS works; the script only uses the standard library.
- **AWS CLI** with credentials that can run `ec2:DescribeInstances` in `us-east-1`.
- **SSH access** as `tiostech` to the instances' private IPs. This usually means you are
  on the VPN with your key loaded.

### Deploying

```bash
git checkout master
git pull
bin/deploy
```

You can set these variables to change the defaults:

| Variable | Effect |
| --- | --- |
| `DEPLOY_ENV=staging` | Targets instances tagged with a different `env` (default `production`). |
| `ALLOW_NON_MASTER=1` | Deploys the current branch instead of requiring `master`. The branch must be pushed. |

If any host fails (SSH, `git pull`, `pip install`, or `systemctl restart`), the script
lists the failed IPs at the end and exits non-zero. The other hosts still get the new
version, so fix the problem and re-run the deploy.

### Server-side configuration

The deploy script does not provision the servers; it assumes the instance already exists
and is configured:

- `config/mysql.yml` on each server is provisioned by Salt from the secrets dir
  (`regression-tool-appconfig/mysql.yml`) and includes the password, so users never see a
  password prompt.
- The instance, the `.venv`, the checkout under `/ebsmount/www/`, and the
  `regression-tool` systemd unit are all set up when the instance is launched (tiosaws
  launcher, primary name `regressiontool`, plus Salt).

To change the DB credentials on the servers, update the Salt secret. Do not edit the file
on the box, because Salt will overwrite it.
