from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from app.db import run_select
from app.graph import get_graph, initial_state


def norm(rows):
    def cell(v):
        return round(float(v), 2) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v).strip().lower()
    return sorted(tuple(cell(v) for v in r) for r in rows)


def main() -> int:
    cases = json.loads((Path(__file__).parent.parent / "tests" / "golden.json").read_text(encoding="utf-8"))
    ok = 0
    for i, c in enumerate(cases, 1):
        t0 = time.time()
        st = get_graph().invoke(initial_state(c["q"], f"eval-{i}", "", "admin"))
        try:
            expected = norm(run_select(c["ref"], 500)["rows"])
            got = norm(st.get("rows", []))
            # compare on the values only; extra columns in the answer are tolerated if expected is a subset per row
            passed = got == expected or (
                len(got) == len(expected) and all(set(e) <= set(g) for e, g in zip(expected, got)))
        except Exception as e:  # noqa: BLE001
            passed, expected = False, str(e)
        ok += passed
        print(f"[{'PASS' if passed else 'FAIL'}] {c['q']}  ({time.time() - t0:.1f}s, retries={st.get('attempts', 0)})")
        if not passed:
            print("   sql     :", st.get("sql"))
            print("   expected:", str(expected)[:200])
            print("   got     :", str(norm(st.get('rows', [])))[:200])
    print(f"\nAccuracy: {ok}/{len(cases)} = {ok / len(cases):.0%}")
    return 0 if ok == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
