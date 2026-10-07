"""Convert only a path selected by the trusted desktop picker to an opaque grant."""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "bridge"))

from advisor_contracts import AdvisorError
from advisor_imports import issue_local_grant

try:
    if len(sys.argv) != 2:
        raise AdvisorError("invalid-request", "请选择技能包")
    print(json.dumps(issue_local_grant(root, sys.argv[1]), ensure_ascii=True))
except AdvisorError as error:
    print(json.dumps({"error": error.message}, ensure_ascii=True))
    raise SystemExit(1)
except Exception:
    print(json.dumps({"error": "技能包读取失败，请检查文件"}, ensure_ascii=True))
    raise SystemExit(1)
