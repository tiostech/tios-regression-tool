#!/bin/bash
cd "$(dirname "$0")"

echo "🧹 Sanitizing search paths to bypass old pyenv configurations..."
# Temporarily remove pyenv shims from this terminal session's PATH
export PATH=$(echo "$PATH" | tr ':' '\n' | grep -v "\.pyenv" | tr '\n' ':' | sed 's/:$//')

# Pinpoint a safe, native, or Homebrew version of Python 3
if [ -x "/opt/homebrew/bin/python3" ]; then
    PYTHON_EXE="/opt/homebrew/bin/python3"
elif [ -x "/usr/bin/python3" ]; then
    PYTHON_EXE="/usr/bin/python3"
elif [ -x "/usr/local/bin/python3" ]; then
    PYTHON_EXE="/usr/local/bin/python3"
else
    PYTHON_EXE="python3"
fi

PY_VERSION=$($PYTHON_EXE -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "🔎 Safely isolated Python instance: $PY_VERSION via $PYTHON_EXE"

# Drop out completely if it still resolves to an unbuildable legacy version
if [[ "$PY_VERSION" == "3.7" || "$PY_VERSION" == "3.6" || "$PY_VERSION" == "3.5" ]]; then
    echo "❌ Error: System is locked into an old Python version ($PY_VERSION)."
    echo "Please open a regular terminal window and run: brew install python"
    exit 1
fi

# Always scrub any existing environment built on the wrong architecture
if [ -d ".venv" ]; then
    echo "Scrubbing previous virtual environment baseline..."
    rm -rf .venv
fi

echo "Rebuilding isolated sandbox with Python..."
$PYTHON_EXE -m venv .venv

echo "Activating sandbox..."
source .venv/bin/activate

echo "Checking and installing required packages (Pre-compiled Wheels)..."
pip install --upgrade pip --quiet
pip install streamlit pandas numpy plotly xgboost scikit-learn pymysql sqlalchemy cryptography pyyaml statsmodels --quiet

echo "Launching GenForecast Pro..."
streamlit run app.py