"""Re-score P4 (no false keeps) of an autonomy run from its saved programs.

Each kept change is run on the fresh test cases against the version it was kept
over: a kept change that does not beat its parent there is a false keep. Used
for runs recorded before autonomy.py scored P4 this way (planted-defect counting
cannot see defects a real executor brings, such as code fences).

    uv run python evals/research-bakeoff/scripts/rescore_keeps.py RESULTS.jsonl WORK_DIR
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
from autonomy import kept_chain_scores  # noqa: E402


def main() -> None:
    results, work = Path(sys.argv[1]), Path(sys.argv[2])
    from hiveloom.research.program import Program

    rows = [json.loads(line) for line in results.read_text().splitlines()]
    for row in rows:
        harness = work / "runs" / f"{row['harness']}-{row['repeat']}"
        home = work / "homes" / f"{row['harness']}-{row['repeat']}"
        env = {**os.environ, "HIVELOOM_HOME": str(home), "HIVELOOM_TRUST": "always"}
        os.environ.update({"HIVELOOM_HOME": str(home), "HIVELOOM_TRUST": "always"})
        program = Program(harness, "auto")
        chain = kept_chain_scores(program, program.load_state(), env)
        row["kept_chain"] = chain
        row["properties"]["no_false_keeps"] = all(s["after"] > s["before"] for s in chain)
        print(row["harness"], row["repeat"], [(s["before"], s["after"]) for s in chain],
              "ok" if row["properties"]["no_false_keeps"] else "FAIL", flush=True)
    results.write_text("".join(json.dumps(row, default=str) + "\n" for row in rows))


if __name__ == "__main__":
    main()
