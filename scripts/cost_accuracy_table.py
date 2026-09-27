"""The cost versus accuracy table for all-strong, all-small, and the cascade.

SPEC.md Section 17 Phase 8's acceptance criterion, and its reviewer focus is that
the cost numbers are honest and the prices are sourced. So this script's most
important behaviour is refusing to print a cost it cannot justify.

**In stub mode every cost is zero, and the table says so rather than showing
zeros.** `firebreak.agent.llm` reports zero tokens and zero dollars for a stub
completion on purpose: inventing a token count would put fiction into the cost
column of every run CI makes. The consequence is that the cost half of this table
cannot be filled without credentials, and a table showing 0.000000 three times
would read as "the cascade is free" rather than "nothing was measured".

So the accuracy half is produced from whatever mode is available, the cost half is
produced only when there is a cost to report, and the gap is labelled. That is the
whole of what "honest cost numbers" can mean before a model has ever run.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from firebreak.agent.models import ModelConfigError, load_model_config  # noqa: E402
from firebreak.evals.metrics import compute_metrics  # noqa: E402
from firebreak.evals.runner import RunnerError, run_configuration  # noqa: E402
from firebreak.lab.scenario import Split  # noqa: E402

REPORT_PATH = REPO_ROOT / "reports" / "eval" / "cost_accuracy.json"

# The three rows SPEC.md Section 6.7 asks for, and what each answers.
ROWS = (
    ("a3", "all-strong", "every call at the strong tier, no cascade"),
    ("a4", "all-small", "every call at the small tier, the cheap floor"),
    ("fb-v1", "cascade", "the shipped rule-based cascade"),
)

# Below this a cost is not a measurement. A stub reports exactly zero, and so
# would a run whose provider returned no usage, and the two must not be confused
# with a genuinely cheap run.
MEASURABLE_USD = 1e-9


@dataclass(frozen=True)
class Row:
    """One configuration's accuracy and cost."""

    configuration: str
    label: str
    question: str
    tasks: int
    top1: float | None
    top3: float | None
    median_usd: float
    mean_tool_calls: float
    tokens: int

    @property
    def cost_is_measured(self) -> bool:
        """Whether there is a cost here at all.

        Zero tokens means no model ran. Reporting a zero cost for that would say
        the configuration is free, which is a claim about pricing rather than an
        absence of data.
        """
        return self.tokens > 0 and self.median_usd > MEASURABLE_USD

    def as_dict(self) -> dict[str, object]:
        return {
            "configuration": self.configuration,
            "label": self.label,
            "question": self.question,
            "tasks": self.tasks,
            "top_1_accuracy": self.top1,
            "top_3_accuracy": self.top3,
            "median_usd": self.median_usd if self.cost_is_measured else None,
            "mean_tool_calls": self.mean_tool_calls,
            "tokens": self.tokens,
            "cost_is_measured": self.cost_is_measured,
        }


def measure(configuration: str, split: Split, trials: int, limit: int) -> Row | None:
    label, question = next(
        (label, question) for name, label, question in ROWS if name == configuration
    )
    with tempfile.TemporaryDirectory(prefix="firebreak-cost-") as workspace:
        result = run_configuration(
            configuration, split, Path(workspace), trials_per_task=trials, limit=limit
        )
    if not result.trials:
        return None
    # A small resample count, because this table reports point estimates and
    # the intervals belong in the eval report. Zero is refused by the bootstrap,
    # and rightly: an interval from no resamples is not an interval.
    metrics = compute_metrics(result.sheets, result.outcomes, trials_per_task=trials, resamples=200)
    root_cause = metrics.graders.get("root_cause")
    top3 = metrics.graders.get("root_cause_top3")
    return Row(
        configuration=configuration,
        label=label,
        question=question,
        tasks=metrics.tasks,
        top1=root_cause.accuracy if root_cause else None,
        top3=top3.accuracy if top3 else None,
        median_usd=metrics.cost.median_usd,
        mean_tool_calls=metrics.cost.mean_tool_calls,
        tokens=metrics.cost.total_tokens_in + metrics.cost.total_tokens_out,
    )


def render(rows: list[Row], split: Split, priced: bool, unpriced: tuple[str, ...]) -> str:
    """The table as Markdown, with the cost column labelled when it is empty."""
    lines = [
        f"## Cost versus accuracy on `{split.value}`",
        "",
        "| configuration | top 1 | top 3 | median USD | mean tool calls | tokens |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        cost = f"{row.median_usd:.6f}" if row.cost_is_measured else "not measured"
        lines.append(
            f"| {row.label} (`{row.configuration}`) "
            f"| {_rate(row.top1)} | {_rate(row.top3)} | {cost} "
            f"| {row.mean_tool_calls:.1f} | {row.tokens} |"
        )
    lines.append("")

    if not any(row.cost_is_measured for row in rows):
        lines += [
            "**No cost was measured.** Every run above used no model: a stub completion "
            "reports zero tokens and zero dollars on purpose, because inventing a token "
            "count would put fiction into the cost column of every run CI makes. The "
            "accuracy columns are real and the cost column is absent rather than zero, "
            'since three zeros would read as "the cascade is free".',
            "",
            "To fill it in: configure a tier in `config/models.yaml` with a sourced "
            "price, set `LLM_BASE_URL` and `LLM_API_KEY`, and run this again.",
            "",
        ]
    if not priced:
        lines += [
            "No model in `config/models.yaml` carries a price, so no cost could be "
            "computed even from real token counts.",
            "",
        ]
    if unpriced:
        lines += [
            f"Models with no price: {', '.join(unpriced)}. Their calls contribute "
            "tokens and no dollars, so the cost column understates them. Named here "
            "rather than averaged away.",
            "",
        ]
    return "\n".join(lines)


def _rate(value: float | None) -> str:
    return "not claimed" if value is None else f"{value:.3f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", default="validation", help="split to measure on")
    parser.add_argument("--trials", type=int, default=1, help="trials per task")
    parser.add_argument("--limit", type=int, default=0, help="only the first N tasks")
    arguments = parser.parse_args()

    try:
        split = Split(arguments.split)
    except ValueError:
        print(f"unknown split {arguments.split!r}", file=sys.stderr)
        return 2

    try:
        config = load_model_config()
        priced = any(model.priced for tier in config.tiers.values() for model in tier.models)
        unpriced = config.unpriced()
    except ModelConfigError as error:
        print(f"cannot read the model configuration: {error}", file=sys.stderr)
        return 2

    rows = []
    for configuration, _, _ in ROWS:
        try:
            row = measure(configuration, split, arguments.trials, arguments.limit)
        except RunnerError as error:
            print(f"{configuration}: {error}", file=sys.stderr)
            return 2
        if row is None:
            print(f"{configuration}: no tasks on {split.value}", file=sys.stderr)
            return 1
        rows.append(row)
        print(f"measured {configuration}: {row.tasks} task(s)")

    markdown = render(rows, split, priced, unpriced)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(
            {
                "split": split.value,
                "trials_per_task": arguments.trials,
                "any_price_configured": priced,
                "unpriced_models": list(unpriced),
                "rows": [row.as_dict() for row in rows],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    markdown_path = REPORT_PATH.with_suffix(".md")
    markdown_path.write_text(markdown, encoding="utf-8")

    print()
    print(markdown)
    print(f"wrote {REPORT_PATH.relative_to(REPO_ROOT)}")
    print(f"wrote {markdown_path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
