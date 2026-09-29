"""Score a model's reader judgement against the cases in EXAMPLES_CH.md.

Not in the shipped payload, and not in the engine checks either: this one cannot run without a model
in the loop, so it is a measurement, not a test. `emit` prints what to hand a model; `score` reads
what it answered; `selftest` proves the scorer can fail before anyone trusts its percentage.

    python release/build/reader_eval.py check                 is the case set well formed
    python release/build/reader_eval.py emit                  the prompt to hand a model
    python release/build/reader_eval.py score answers.json    score what a model answered
    python release/build/reader_eval.py selftest              prove the scorer can fail
"""
import json
import os
import re
import sys

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
CASES = os.path.join(HERE, "reader-cases.json")
TASKS = os.path.join(REPO, "EXAMPLES_CH.md")
SEATS = ("developer", "deployer", "user", "maintainer")
ITEM = re.compile(r"^(\d+)\. ", re.M)


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def cases():
    return sorted(json.loads(read_text(CASES)), key=lambda case: case["n"])


def tasks():
    """The task text, keyed by the number the example file gives it."""
    text = read_text(TASKS)
    heads = list(ITEM.finditer(text))
    found = {}
    for index, head in enumerate(heads):
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        found[int(head.group(1))] = text[head.start():end].strip()
    return found


def rule_text():
    """The live rule, so the measurement is of the rule as it stands rather than of a copy of it."""
    sys.path.insert(0, os.path.join(REPO, ".github", "ocf"))
    import ocf
    policy, _ = ocf.load_policy(REPO)
    return ocf.soft_text(ocf.rule_by_id(policy, "write-for-the-reader"), policy).strip()


def check():
    problems = []
    judged = cases()
    numbers = [case["n"] for case in judged]
    if numbers != list(range(1, len(numbers) + 1)):
        problems.append("the case numbers are not 1..%d in order: %s" % (len(numbers), numbers))
    for case in judged:
        unknown = [seat for seat in case["seats"] if seat not in SEATS]
        if unknown:
            problems.append("case %d names a seat the rule does not define: %s" % (case["n"], unknown))
        if not case["seats"]:
            problems.append("case %d names no reader at all" % case["n"])
    written = tasks()
    missing = [case["n"] for case in judged if case["n"] not in written]
    if missing:
        problems.append("no task text in EXAMPLES_CH.md for: %s" % missing)
    extra = sorted(number for number in written if number > len(judged))
    if extra:
        problems.append("EXAMPLES_CH.md has tasks with no case: %s" % extra)
    return problems


def emit():
    print(rule_text())
    print()
    print("Name the reader of each task below. Answer with a JSON object: the number as a string,")
    print('the seats as a list, for example {"1": ["developer"], "2": ["developer", "user"]}.')
    print()
    for number, text in sorted(tasks().items()):
        print(text)
        print()


def score(answers, quiet=False):
    judged = cases()
    hits = 0
    misses = []
    for case in judged:
        said = answers.get(str(case["n"]))
        if said is not None and set(said) == set(case["seats"]):
            hits += 1
        else:
            misses.append((case["n"], case["seats"], said))
    total = len(judged)
    if not quiet:
        print("%d of %d exact (%d%%)" % (hits, total, round(100.0 * hits / max(total, 1))))
        for number, wanted, said in misses:
            shown = "unanswered" if said is None else ",".join(said) or "nothing named"
            print("  MISS %-3d wanted %-30s said %s" % (number, ",".join(wanted), shown))
        hard_missed = sum(1 for number, _, _ in misses
                          if any(case["n"] == number and case["hard"] for case in judged))
        print("  %d of the misses are on the cases marked hard" % hard_missed)
    return hits, total


def selftest():
    """Prove the scorer can fail: a perfect answer must score perfect, a damaged one must not."""
    perfect = {str(case["n"]): list(case["seats"]) for case in cases()}
    good, total = score(perfect, quiet=True)
    if good != total:
        print("selftest FAILED: the reference answers did not score full marks")
        return 1
    damaged = {number: seats[1:] for number, seats in perfect.items()}
    bad, _ = score(damaged, quiet=True)
    if bad >= total:
        print("selftest FAILED: the scorer gave a damaged answer set full marks")
        return 1
    dropped = sum(1 for seats in perfect.values() if len(seats) > 1)
    print("selftest PASS: %d/%d on the reference, %d/%d once the first seat is dropped from every case"
          % (good, total, bad, total))
    print("(%d of the %d cases have more than one reader, so a scorer that ignored order would not"
          " move at all)" % (dropped, total))
    return 0


def main(argv):
    if not argv:
        print(__doc__.strip())
        return 0
    command = argv[0]
    if command == "check":
        problems = check()
        for problem in problems:
            print("  %s" % problem)
        print("%d problem(s)" % len(problems))
        return 1 if problems else 0
    if command == "emit":
        emit()
        return 0
    if command == "score":
        if len(argv) < 2:
            print("score needs a file of answers")
            return 2
        good, total = score(json.loads(read_text(argv[1])))
        return 0 if good == total else 1
    if command == "selftest":
        return selftest()
    print("unknown command: %s" % command)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
