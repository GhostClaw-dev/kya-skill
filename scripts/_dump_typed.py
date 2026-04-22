import sys
from pathlib import Path
dump_to = sys.argv[1]
args = sys.argv[2:]
if "--data" in args:
    idx = args.index("--data") + 1
    Path(dump_to).write_text(args[idx], encoding="utf-8")
