"""UTF-8 JSON command line interface.

Usage:
    python -m counterweir audit   case.json [-o result.json]
    python -m counterweir repair  case.json [-o result.json]
    python -m counterweir verify  case.json claimed_result.json [-o verdict.json]
"""

from __future__ import annotations

import argparse
import json
import sys

from .audit import run_audit
from .model import ModelError, build_components, load_case
from .repair import run_repair
from .verify import run_verify


def _emit(payload: dict, out_path: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        sys.stdout.buffer.write(text.encode("utf-8") + b"\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="counterweir",
        description="Synthetic sealed-receipt sequence audit")
    sub = parser.add_subparsers(dest="command", required=True)

    p_audit = sub.add_parser("audit", help="audit every receipt")
    p_audit.add_argument("case_path")
    p_audit.add_argument("-o", "--output")

    p_repair = sub.add_parser("repair", help="choose observations to pause")
    p_repair.add_argument("case_path")
    p_repair.add_argument("-o", "--output")

    p_verify = sub.add_parser("verify", help="re-solve and check a claim")
    p_verify.add_argument("case_path")
    p_verify.add_argument("claimed_path")
    p_verify.add_argument("-o", "--output")

    args = parser.parse_args(argv)
    try:
        case = load_case(args.case_path)
        model = build_components(case)
    except (ModelError, json.JSONDecodeError, KeyError, TypeError) as exc:
        payload = {"accepted": False, "errors": [f"input error: {exc}"]}
        _emit(payload, getattr(args, "output", None))
        return 2

    if args.command == "audit":
        _emit(run_audit(model), args.output)
    elif args.command == "repair":
        _emit(run_repair(model), args.output)
    else:
        with open(args.claimed_path, "r", encoding="utf-8") as fh:
            claimed = json.load(fh)
        verdict = run_verify(model, claimed)
        _emit(verdict, args.output)
        return 0 if verdict["accepted"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
