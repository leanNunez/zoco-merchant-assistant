"""`python -m evaluation.harness`: the single evaluation command."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from evaluation.harness.suite import RESULTS, all_cases, provenance, run_cases, write_results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the harness: 13 kit scenarios + variants"
    )
    parser.add_argument("--only", nargs="+", metavar="NAME", help="run only these scenarios")
    parser.add_argument("--jobs", type=int, default=4, help="scenarios run in parallel")
    parser.add_argument("--date", help="the date of the results folder (default: today, UTC)")
    parser.add_argument(
        "--out", type=Path, help="write the results here instead of the dated folder"
    )
    parser.add_argument("--no-write", action="store_true", help="print only; write no files")
    args = parser.parse_args(argv)

    cases = all_cases()
    if args.only:
        unknown = set(args.only) - {case.name for case in cases}
        if unknown:
            parser.error(f"unknown scenarios: {sorted(unknown)}")
        cases = [case for case in cases if case.name in set(args.only)]

    outcomes = run_cases(cases, max(1, args.jobs))
    for outcome in outcomes:
        verdict = "PASS" if outcome.passed else "FAIL"
        print(f"{verdict}  {outcome.name:<46} {len(outcome.checks):>3} checks")
        for check in outcome.failures:
            print(f"        - {check.name}: {check.detail}")
    passed = sum(outcome.passed for outcome in outcomes)
    print(f"\n{passed}/{len(outcomes)} PASS")

    if not args.no_write and not args.only:
        date = args.date or datetime.now(UTC).date().isoformat()
        folder = args.out or RESULTS / date
        write_results(outcomes, provenance(date), folder)
        print(f"results: {folder}")
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
