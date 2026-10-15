import sys, logging, pandas as pd
logging.disable(logging.WARNING)
sys.path.insert(0, '.')
from lib.db import standalone_engine
from sqlalchemy import text
pd.set_option('display.width', 250); pd.set_option('display.max_columns', 40); pd.set_option('display.max_colwidth', 70); pd.set_option('display.max_rows', 300)
eng = standalone_engine()
for stmt in [s for s in sys.stdin.read().split(';;') if s.strip()]:
    print('>>', ' '.join(stmt.split())[:140])
    try:
        print(pd.read_sql(text(stmt), eng).to_string())
    except Exception as e:
        print('ERROR', str(e.__cause__ or e).split('\n')[0][:200])
    print()
