"""Explicit dry-run-first rollback of one notice parse."""

import argparse
from datetime import datetime, timezone
from uuid import uuid4

from . import db


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("notice_id")
    parser.add_argument("parse_id")
    parser.add_argument("--reason", default="operator rollback")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    with db.get_conn() as conn:
        parse = conn.execute(
            "SELECT notice_id FROM notice_parses WHERE parse_id = ?",
            [args.parse_id],
        ).fetchone()
    if parse is None or parse["notice_id"] != args.notice_id:
        raise ValueError("parse does not belong to notice")
    if not args.apply:
        print(
            f"DRY-RUN: roll back {args.notice_id} to {args.parse_id}"
        )
        return 0
    db.rollback_notice_parse(
        args.notice_id,
        args.parse_id,
        args.reason,
        datetime.now(timezone.utc).isoformat(),
        uuid4().hex,
    )
    print(f"Rolled back {args.notice_id} to {args.parse_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
