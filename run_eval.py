"""Run the local, deterministic customer-service trial evaluation set."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import app


ROOT = Path(__file__).parent


def main() -> int:
    cases = json.loads((ROOT / "eval_cases.json").read_text(encoding="utf-8"))
    passed = 0
    failures = []
    with tempfile.TemporaryDirectory() as directory:
        db = app.StoreDB(Path(directory) / "eval.sqlite3")
        service = app.CustomerService(db)
        for case in cases:
            result = service.chat({"message": case["message"]})
            mode_ok = result["reply_mode"] == case["mode"]
            text_ok = case["contains"] in result["answer"]
            if mode_ok and text_ok:
                passed += 1
            else:
                failures.append({"id": case["id"], "expected": case, "actual": {"mode": result["reply_mode"], "answer": result["answer"]}})
        db.close()
    report = {"total": len(cases), "passed": passed, "failed": len(failures), "failures": failures}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
