"""Read shortened copies of actual CPU records, without running the project."""
# BEGIN example
import json

for name in ("cpu_success_excerpt.json", "cpu_failure_excerpt.json"):
    with open(name, encoding="utf-8") as stream:
        record = json.load(stream)
    print(record["status"])
    failure = record["failure"]
    if failure is None:
        print("Geometric plan only. Physics is not checked here.")
    else:
        print(failure["type"] + ": " + failure["message"])
# END example
