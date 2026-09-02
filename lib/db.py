"""Database access for the Trader Custom Tools app.

Every page follows the same two lines:

    from lib import db

    if not db.gate():      # resolves credentials, prompts only if it has to
        st.stop()
    df = pd.read_sql(query, db.engine())

Connection settings come from ``config/mysql.yml`` -- provisioned by Salt on the
servers, copied from ``config/mysql.yml.sample`` for local dev.

The password is resolved in three steps, first hit wins:

    1. ``mysql_slave.password`` in config/mysql.yml  (how the servers work)
    2. the ``TIOS_DB_PASSWORD`` environment variable  (handy for local dev)
    3. a prompt in the sidebar                        (local fallback)

That means nobody on a server ever sees a password box, and local dev still
works with the password left blank in the YAML.

A blank password is a valid answer at every step -- local MySQL accounts often
have none. So "not supplied" is represented by ``None`` and "supplied, empty" by
``""``, and the sidebar prompt uses a Connect button so an empty box submitted
on purpose is distinguishable from one nobody has typed into yet.
"""

import os

import streamlit as st
import yaml
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Widget key for the fallback password box. Streamlit persists widget values in
# session_state under their key, so this doubles as the per-session store.
_PASSWORD_KEY = "db_password"

# Set once the user presses Connect. Needed because a blank box is a legitimate
# answer -- without this flag there is no way to tell "the account has no
# password" from "the user has not typed anything yet".
_SUBMITTED_KEY = "db_password_submitted"

PASSWORD_ENV_VAR = "TIOS_DB_PASSWORD"

DEFAULTS = {
    "host": "127.0.0.1",
    "port": 3309,
    "database": "tioscore_production",
}


class ConfigError(RuntimeError):
    """config/mysql.yml is missing, unparseable, or lacks a username."""


def config_path():
    """Absolute path to config/mysql.yml."""
    return os.path.join(_REPO_ROOT, "config", "mysql.yml")


def load_mysql_config():
    """
    Returns the ``mysql_slave`` block from config/mysql.yml.

    Expected structure:
        mysql_slave:
          host: 127.0.0.1
          port: 3309
          database: tioscore_production
          username: <user>
          password: <pass>     # may be blank for local dev

    Raises ConfigError -- with a message worth showing to a user -- if the file
    is missing, malformed, or has no username.
    """
    path = config_path()

    if not os.path.exists(path):
        raise ConfigError(
            f"Config file not found at {path}. Copy config/mysql.yml.sample to "
            "config/mysql.yml and fill in the connection settings."
        )

    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        raise ConfigError(f"Could not parse {path}: {e}")

    cfg = data.get("mysql_slave") or {}
    if not cfg.get("username"):
        raise ConfigError(f"{path} is missing mysql_slave: username.")

    return cfg


def settings():
    """Returns (username, host, port, database). Never includes the password."""
    cfg = load_mysql_config()
    return (
        cfg["username"],
        cfg.get("host") or DEFAULTS["host"],
        cfg.get("port") or DEFAULTS["port"],
        cfg.get("database") or DEFAULTS["database"],
    )


def password_source():
    """
    Returns (password, source) where source is "config", "env", "session", or
    None if no password has been supplied yet.

    An empty string is a real answer -- it means "this account has no password",
    which is normal for a local dev MySQL. Only ``None`` means "nothing supplied
    yet", so callers must test ``password is None``, not ``not password``.
    """
    try:
        cfg = load_mysql_config()
    except ConfigError:
        cfg = {}

    # In YAML, `password:` with nothing after it parses as None, which means
    # "not set". Write `password: ""` to declare a genuinely blank password.
    if cfg.get("password") is not None:
        return str(cfg["password"]), "config"

    from_env = os.environ.get(PASSWORD_ENV_VAR)
    if from_env is not None:
        return from_env, "env"

    if st.session_state.get(_SUBMITTED_KEY):
        return st.session_state.get(_PASSWORD_KEY) or "", "session"

    return None, None


@st.cache_resource(show_spinner=False)
def _engine(username, password, host, port, database):
    """
    The actual engine, cached on the full credential set.

    ``cache_resource`` means one engine -- and one connection pool -- per set of
    credentials for the whole app process, shared across pages, tabs, and users.
    Building an engine per button click throws the pool away each time.
    """
    url = URL.create(
        "mysql+pymysql",
        username=username,
        password=password,
        host=host,
        port=port,
        database=database,
    )

    # URL.create escapes the password, so passwords containing @ : / # ? work.
    return create_engine(url, pool_pre_ping=True, pool_recycle=3600)


def engine():
    """
    The app's SQLAlchemy engine. Call this anywhere you need a connection.

    Raises ConfigError if no password has been resolved yet -- call ``gate()``
    first so the user gets a prompt instead of a traceback.
    """
    username, host, port, database = settings()
    password, _ = password_source()

    # `password is None` means nothing has been supplied. An empty string has
    # been supplied and means the account has no password, so let it through.
    if password is None:
        raise ConfigError(
            f"No database password available. Set mysql_slave.password in "
            f"{config_path()}, export {PASSWORD_ENV_VAR}, or enter it in the sidebar."
        )

    return _engine(username, password, host, port, database)


@st.cache_data(ttl=60, show_spinner=False)
def check(_credential_fingerprint):
    """
    Runs SELECT 1. Returns None on success, or the error message on failure.

    Cached for a minute so a rerun does not re-probe on every widget change,
    but a dropped tunnel still surfaces quickly. The fingerprint argument is
    only there to bust the cache when the credentials change.
    """
    try:
        with engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return None
    except Exception as e:
        return str(e)


def reset():
    """
    Drops the cached engine and every cached query result.

    Use after editing config/mysql.yml, or to force a reload of reference data
    that has gone stale.
    """
    st.cache_resource.clear()
    st.cache_data.clear()


def gate(label="1. Database"):
    """
    Renders the sidebar connection block and returns True when the app is ready
    to query.

    Shows a password box only when steps 1 and 2 of the resolution order came up
    empty -- so on the servers this is just a status line and a Reload button.
    Callers should ``st.stop()`` when this returns False.
    """
    st.sidebar.header(label)

    try:
        username, host, port, database = settings()
    except ConfigError as e:
        st.sidebar.error(str(e))
        return False

    st.sidebar.caption(f"{username}@{host}:{port}/{database}")

    password, source = password_source()

    if source in (None, "session"):
        # Nothing from the config file or the environment, so ask. A form is used
        # rather than a bare text_input so that pressing Connect on an empty box
        # is an explicit "this account has no password" rather than ambiguous.
        with st.sidebar.form("db_password_form"):
            st.text_input("Password", type="password", key=_PASSWORD_KEY)
            if st.form_submit_button("Connect"):
                st.session_state[_SUBMITTED_KEY] = True

        if not st.session_state.get(_SUBMITTED_KEY):
            st.sidebar.caption(
                "Press Connect. Leave the box blank if the account has no password."
            )
            return False

        password = st.session_state.get(_PASSWORD_KEY) or ""

    if st.sidebar.button("Reload data", key=f"reload_{label}"):
        reset()
        st.rerun()

    # Actually prove the connection works. Without this, an unreachable database
    # shows up as silently empty dropdowns further down the page.
    failure = check(f"{username}@{host}:{port}/{database}/{hash(password)}")
    if failure:
        st.sidebar.error(f"Cannot reach the database: {failure}")
        return False

    return True
