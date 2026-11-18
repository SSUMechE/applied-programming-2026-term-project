"""Complete classroom batch. Numerical fixtures, not feasible robot motions."""
from itertools import product
from time import perf_counter
from pathlib import Path
import json
import numpy as np


# BEGIN supplied
def example_path(peak):
    path = np.zeros((3, 7), dtype=float)
    path[:, 0] = [0.0, peak, 0.6]
    return path


paths = {("A", "case_1"): example_path(0.7),
         ("A", "case_2"): None,
         ("B", "case_1"): example_path(0.75),
         ("B", "case_2"): example_path(0.8)}
# END supplied


# BEGIN calculation
def measure_path(path):
    if path is None:
        raise ValueError("no path returned")
    q = np.asarray(path, dtype=float)
    if q.ndim != 2 or q.shape[1] != 7 or len(q) < 2:
        raise ValueError("expected at least two seven-joint rows")
    if not np.isfinite(q).all():
        raise ValueError("joint angles must be finite")
    return float(np.linalg.norm(np.diff(q, axis=0), axis=1).sum())
# END calculation


def main():
    destination = Path("demo_runs.jsonl")
    previous = []
    if destination.exists():
        previous = [json.loads(line) for line in
                    destination.read_text(encoding="utf-8").splitlines()]
    attempt = max((r["attempt"] for r in previous), default=0) + 1
    settings, cases = ("A", "B"), ("case_1", "case_2")

    # BEGIN execution
    records = []
    for setting, case in product(settings, cases):
        started = perf_counter()
        try:
            length = measure_path(paths[setting, case])
            status, error = "ok", None
        except ValueError as exc:
            length, status, error = None, "failed", str(exc)
        elapsed = perf_counter() - started
        record = {"attempt": attempt, "setting": setting,
                  "case_id": case, "status": status, "error": error,
                  "joint_length_rad": length, "elapsed_s": elapsed}
        with destination.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, allow_nan=False) + "\n")
        records.append(record)
        shown = f"{length:.2f}" if length is not None else "None"
        print(setting, case, status, shown)
    # END execution

    # BEGIN summary
    for setting in settings:
        group = [r for r in records if r["setting"] == setting]
        lengths = [r["joint_length_rad"] for r in group
                   if r["status"] == "ok"]
        mean = sum(lengths) / len(lengths) if lengths else None
        shown = f"{mean:.2f}" if mean is not None else "None"
        print(setting, len(lengths), "/", len(group), shown)
    print("Appended 4 records to demo_runs.jsonl")
    # END summary


if __name__ == "__main__":
    main()
