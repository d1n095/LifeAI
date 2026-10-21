"""MI-1..MI-4: migration integrity for a candidate (static; no database).
  MI-1 exactly one Alembic head;
  MI-2 no duplicate revision ids;
  MI-3 no two revisions share a numeric prefix (e.g. two different `0089_*` — the #280 vs #281 collision);
  MI-4 the chain from head to base is linear (every revision reachable, no orphan branch).
Usage: python check.py <candidate-backend-dir>   Prints pytest-style PASSED/FAILED lines for summarize.py."""
import re, sys, collections
from pathlib import Path

backend = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(backend))
from alembic.config import Config
from alembic.script import ScriptDirectory

cfg = Config(str(backend / "alembic.ini"))
cfg.set_main_option("script_location", str(backend / "alembic"))
sd = ScriptDirectory.from_config(cfg)
revs = list(sd.walk_revisions())
ids = [r.revision for r in revs]
heads = sd.get_heads()
res = {}
res["MI-1_single_head"] = (len(heads) == 1, f"heads={heads}")
dups = [k for k, v in collections.Counter(ids).items() if v > 1]
res["MI-2_unique_revision_ids"] = (not dups, f"duplicates={dups}")
prefix = collections.defaultdict(list)
for r in ids:
    m = re.match(r"^(\d{4})", r)
    if m:
        prefix[m.group(1)].append(r)
clash = {k: v for k, v in prefix.items() if len(v) > 1}
res["MI-3_unique_numeric_prefix"] = (not clash, f"clashes={clash}")
chain, cur = [], sd.get_revision(heads[0]) if len(heads) == 1 else None
while cur is not None:
    chain.append(cur.revision)
    dn = cur.down_revision
    if isinstance(dn, (tuple, list)):
        res["MI-4_linear_chain"] = (False, f"merge point at {cur.revision}: {dn}")
        break
    cur = sd.get_revision(dn) if dn else None
res.setdefault("MI-4_linear_chain", (len(chain) == len(ids), f"chain={len(chain)} revisions={len(ids)}"))
print(f"MI|head={heads} revisions={len(ids)}")
for k, (ok, detail) in res.items():
    print(f"{'PASSED' if ok else 'FAILED'} migration::{k} {'' if ok else detail}")
sys.exit(0 if all(ok for ok, _ in res.values()) else 1)
