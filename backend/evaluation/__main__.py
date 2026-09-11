"""The single command C9-05 asks for.

    uv run python -m backend.evaluation

Deterministic by default, so it runs anywhere and in CI. `--live` adds the
model-dependent half when a model is configured; without one it refuses rather
than quietly running the deterministic half under a name that promises more.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from backend.evaluation.labels import LabelInvalid
from backend.evaluation.report import render, run_evaluation
from backend.models.session import dispose_engine, init_engine
from backend.shared.config import GeminiConfig, Settings, get_settings
from backend.shared.model_provider import ModelProvider, build_model_provider

DEFAULT_CASES = Path("tests/fixtures/labelled")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.evaluation",
        description="Measure Continuity against labelled fixtures (02_ARCHITECTURE.md §18).",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=DEFAULT_CASES,
        help=f"directory of labelled cases (default: {DEFAULT_CASES})",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="also run the model-dependent half, using the configured model",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="also write the full result as JSON to this path",
    )
    return parser


def _model_provider(settings: Settings, live: bool) -> ModelProvider | None:
    if not live:
        return None
    if not isinstance(settings.gemini, GeminiConfig):
        raise SystemExit(
            "--live needs a configured model, and there is none: "
            f"{settings.gemini.reason}"
        )
    return build_model_provider(settings)


async def _main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    settings = get_settings()

    # Its own database, thrown away afterwards. The evaluation creates projects
    # and runs, and it has no business doing that in whatever database the
    # deployment is pointed at.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="continuity-evaluation-db-") as scratch:
        evaluation_settings = settings.model_copy(
            update={"DATABASE_URL": f"sqlite+aiosqlite:///{Path(scratch) / 'eval.db'}"}
        )
        init_engine(evaluation_settings)
        try:
            from backend.models.session import create_all

            await create_all()
            run = await run_evaluation(
                args.cases,
                model_provider=_model_provider(settings, args.live),
                json_path=args.json,
            )
        finally:
            await dispose_engine()

    print(render(run))
    return 1 if run.failed_cases else 0


def main() -> int:
    try:
        return asyncio.run(_main(sys.argv[1:]))
    except LabelInvalid as exc:
        print(f"labelled case set is unusable: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
