"""CLI: run the LLM auditor over one source's sanitized output.

Reads ``clean-staging/<source>/events.jsonl`` and writes findings to
``logs/audit/<run-id>/<source>.jsonl``. Findings are human-readable
JSON lines — each entry points at one record and describes what the
auditor thinks leaked.

The auditor is local-only (Ollama). The user is responsible for:

1. Installing Ollama (`brew install ollama`).
2. Starting the server (`ollama serve` — usually runs as a background
   service after install).
3. Pulling a model (`ollama pull qwen2.5:14b-instruct`, or
   `qwen2.5:7b-instruct` for faster iteration on smaller hardware).

``AUDIT_MODEL`` env var or ``--model`` flag overrides the default.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from sanitizer.auditor import DEFAULT_MODEL, AuditConfig, AuditorError, audit_source
from scripts._config import LocalConfig, load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        required=True,
        help="Source name (e.g. ical, apple-mail, imessage, local-files).",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("config/local.json"), help="Local config path."
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            f"Ollama model name. Overrides AUDIT_MODEL env var and the {DEFAULT_MODEL!r} default."
        ),
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=100,
        help=(
            "Audit N randomly sampled records instead of the full file. "
            "Default 100 so first runs don't take hours. Pass 0 to audit everything."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed for sampling. Default 0 so repeat runs are deterministic.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="Root of audit logs. Defaults to logs/audit/<run-id>/.",
    )
    args = parser.parse_args(argv)

    cfg: LocalConfig = load_config(args.config)
    events_path = cfg.clean_staging_dir / args.source / "events.jsonl"
    if not events_path.exists():
        print(
            f"error: no sanitized input at {events_path}. Run the sanitize step first.",
            file=sys.stderr,
        )
        return 2

    run_id = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
    log_dir = args.log_dir if args.log_dir else Path("logs") / "audit" / run_id
    log_path = log_dir / f"{args.source}.jsonl"

    config = AuditConfig(
        keep_names=cfg.keep_names,
        keep_gpes=cfg.keep_gpes,
    )

    sample: int | None = args.sample if args.sample and args.sample > 0 else None

    def _progress(done: int, total: int, findings: int) -> None:
        if done % 10 == 0 or done == total:
            pct = (done / total * 100) if total else 0
            print(
                f"  {done}/{total} ({pct:.1f}%) records audited — {findings} findings so far",
                file=sys.stderr,
                flush=True,
            )

    print(
        f"Auditing {args.source}: {events_path}\n"
        f"   → {log_path}\n"
        f"   model: {args.model or 'default (resolve at runtime)'}\n"
        f"   sample: {sample if sample else 'all'}",
        file=sys.stderr,
        flush=True,
    )

    try:
        total = audit_source(
            events_path,
            log_path,
            model_name=args.model,
            config=config,
            sample=sample,
            seed=args.seed,
            progress_callback=_progress,
        )
    except AuditorError as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 2

    # Verifier drop breakdown — tells the user whether the low finding
    # count is "LLM returned nothing" vs "verifier dropped a pile of
    # hallucinations" vs "most findings matched the keep-list".
    stats = getattr(audit_source, "last_stats", None)
    print(f"Wrote {total} finding(s) → {log_path}")
    if stats is not None:
        print(
            f"Verifier stats: raw={stats.raw} kept={stats.kept} "
            f"dropped[hallucinated={stats.hallucinated} "
            f"keep_list={stats.keep_list} "
            f"bracketed_token={stats.bracketed_token} "
            f"empty_excerpt={stats.empty_excerpt}] "
            f"severity_upgraded={stats.severity_upgraded}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
