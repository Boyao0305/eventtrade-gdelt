import sqlite3

import pandas as pd

T = "C:/Users/boyao/.claude/jobs/d07061ba/tmp/"
mp = pd.read_csv("D:/programmation/Github/b2b_approach/team_tool/.claude/worktrees/gdelt-dev/docs/dod/dod_issuer_map.csv", dtype=str)
import sys
bl = pd.read_csv(T + sys.argv[1], dtype=str)
s = sqlite3.connect("file:D:/programmation/Github/b2b_approach/team_tool/workspace/gdelt/data/sec.sqlite?mode=ro", uri=True)
tick = pd.read_sql_query("select cik, ticker from cik_tickers", s)
first = tick.drop_duplicates("cik").set_index("cik")["ticker"].to_dict()
j = bl.merge(mp[["article_id", "seq", "cik", "rule", "ticker_now"]], on=["article_id", "seq"],
             how="left", suffixes=("_blind", "_map"))
j["cik_blind"] = j["cik_blind"].fillna("").map(lambda c: c.zfill(10) if c else "")
j["cik_map"] = j["cik_map"].fillna("")
j["tk_blind"] = j["cik_blind"].map(lambda c: first.get(c, "") if c else "")
j["tk_map"] = j["ticker_now"].fillna("")
j["agree_ticker"] = j["tk_blind"] == j["tk_map"]
j["agree_cik"] = j["cik_blind"] == j["cik_map"]
print("rows", len(j), "agree on priced ticker", int(j["agree_ticker"].sum()),
      "agree on CIK", int(j["agree_cik"].sum()))
d = j[~j["agree_ticker"]]
pd.set_option("display.width", 250)
pd.set_option("display.max_colwidth", 40)
print(d[["release_date", "awardee", "cik_blind", "tk_blind", "reason", "cik_map", "tk_map",
         "rule"]].to_string())
print("CIK-only disagreements (same ticker):")
e = j[j["agree_ticker"] & ~j["agree_cik"]]
print(e[["release_date", "awardee", "cik_blind", "cik_map", "tk_map", "rule"]].to_string())
