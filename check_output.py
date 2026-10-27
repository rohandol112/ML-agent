import sys
from pathlib import Path

from pydantic import ValidationError

from schema import TriageOutput


def check(path):
    try:
        out = TriageOutput.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as exc:
        return [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
    t = out.triage
    problems = []
    if t.is_duplicate != bool(t.duplicate_of):
        problems.append("is_duplicate and duplicate_of disagree")
    if t.needs_more_info != bool(t.missing_info):
        problems.append("needs_more_info and missing_info disagree")
    if t.escalate and not t.escalation_reason:
        problems.append("escalate without escalation_reason")
    if t.notify_account_manager and not t.account_manager_reason:
        problems.append("notify_account_manager without account_manager_reason")
    if not out.steps:
        problems.append("no tool calls recorded")
    if [s.step for s in out.steps] != list(range(1, len(out.steps) + 1)):
        problems.append("steps are not numbered 1..n")
    return problems


if __name__ == "__main__":
    files = [f for arg in sys.argv[1:] or ["outputs"] for f in (sorted(Path(arg).glob("*.json")) if Path(arg).is_dir() else [Path(arg)])]
    failed = 0
    for file in files:
        problems = check(file)
        failed += bool(problems)
        print(f"{'FAIL' if problems else 'PASS'}  {file}" + "".join(f"\n      - {p}" for p in problems))
    print(f"\n{len(files) - failed}/{len(files)} valid")
    sys.exit(1 if failed or not files else 0)
