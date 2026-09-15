"""Render the mandatory OpenClaw Automation directive for immutable instructions."""

from __future__ import annotations

import hashlib
import argparse
import json
import sys
from pathlib import Path


_TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "openclaw-operational-directive-template.md"


def monitor_job_name(discussion_id: str) -> str:
    """Return a stable project-scoped name without trusting user text as shell syntax."""
    digest = hashlib.sha256(discussion_id.encode("utf-8")).hexdigest()[:12]
    return "multiagent-openclaw-%s" % digest


def render_openclaw_operational_directive(workspace: Path, discussion_id: str, coordinator_id: str) -> str:
    """Render the complete mandatory directive embedded in every OpenClaw instruction."""
    template = _TEMPLATE.read_text(encoding="utf-8")
    return (
        template.replace("{{workspace}}", str(Path(workspace).resolve()))
        .replace("{{coordinator_id}}", coordinator_id)
        .replace("{{monitor_job_name}}", monitor_job_name(discussion_id))
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenClaw project automation tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    directive = subparsers.add_parser("directive", help="render the mandatory operational directive")
    directive.add_argument("--workspace", required=True, type=Path)
    directive.add_argument("--discussion-id", required=True)
    directive.add_argument("--coordinator-id", required=True)
    stop = subparsers.add_parser("stop", help="remove and attest this project's OpenClaw cron job")
    stop.add_argument("--workspace", required=True, type=Path)
    stop.add_argument("--instruction-id", required=True)
    stop.add_argument("--private-key", required=True, type=Path)
    stop.add_argument("--automation-job-id")
    stop.add_argument("--key-id", help="optional; must match the key pinned in the stop instruction")
    args = parser.parse_args(argv)

    if args.command == "directive":
        print(render_openclaw_operational_directive(args.workspace, args.discussion_id, args.coordinator_id))
        return 0

    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from adapters.openclaw.stop_adapter import OpenClawStopError, stop_openclaw_automation

    try:
        result = stop_openclaw_automation(
            workspace=args.workspace,
            instruction_id=args.instruction_id,
            private_key_path=args.private_key,
            automation_job_id=args.automation_job_id,
            key_id=args.key_id,
        )
    except (OpenClawStopError, OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
