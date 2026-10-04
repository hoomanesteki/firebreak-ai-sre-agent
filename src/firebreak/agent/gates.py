"""The exit gate: the six checks that decide what a report is allowed to say.

SPEC.md Section 6.9. Code only, no model, and it cannot be bypassed: the only
route from a written report to a published one runs through `run_exit_gate`.

**Why every check is a separate, named result.** A gate that returned a boolean
would be useless to the person whose report just lost four sentences. Each check
reports what it examined, what it rejected, and why, and the results go into the
report so the reader sees "N statements could not be verified and were removed"
with the reasons attached.

**Re-execution is the check that makes the rest mean anything.** Checks 1, 3 and
5 are about internal consistency: a claim citing something, a number matching a
fact, a confidence backed by enough signals. All three can be satisfied by a
report that cites evidence which no longer says what it said. Check 2 re-runs
each cited record against its backend and compares the hash, which is the only
check that catches evidence that has drifted or was never real.

**A failed check triggers one repair pass, then removal.** SPEC.md Section 6.9
is explicit about the order. Repairing twice would let a model that cannot
produce a verifiable claim spend the budget discovering that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import pairwise

from firebreak.agent.state import ClaimType, Confidence, Notebook, Report
from firebreak.tools.evidence import BackendMode, EvidenceKind, EvidenceRecord, Fact

# SPEC.md Section 6.9 check 5: a report claiming more than this must rest on at
# least two signal types. A single metric series is enough to notice something
# and not enough to be sure of it, and a confident report resting on one signal
# is the shape of every plausible wrong answer this system could produce.
HIGH_CONFIDENCE = 0.8
MIN_SIGNAL_TYPES_FOR_HIGH_CONFIDENCE = 2

# How close a number in a claim has to be to the fact it cites. Exact for
# counts, one percent relative for rates and latencies, per SPEC.md Section 6.9
# check 2. Counts are integers and a count that is nearly right is wrong.
COUNT_UNITS = frozenset({"count", "rows", "services"})
RELATIVE_TOLERANCE = 0.01

NUMERIC_LITERAL = re.compile(r"(?<![\w.])[+-]?(?:\d[\d,]*\.?\d*|\.\d+)(?:[eE][+-]?\d+)?(?!\w)")
NUMBER_WORD = re.compile(
    r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion)\b",
    re.IGNORECASE,
)


def tolerance_for(unit: str, value: float) -> float:
    """How far a cited number may sit from the fact it cites.

    SPEC.md Section 6.9: exact for counts, one percent relative for rates and
    latencies. One function because checks 2 and 3 both need the rule and a
    second copy of it would drift.
    """
    return 0.0 if unit in COUNT_UNITS else abs(value) * RELATIVE_TOLERANCE


def _matches(fact: Fact, value: float) -> bool:
    return abs(fact.value - value) <= tolerance_for(fact.unit, fact.value)


class CheckName(StrEnum):
    """The six checks, named so a result can be read without counting."""

    COVERAGE = "coverage"
    RE_EXECUTION = "re_execution"
    NUMBERS = "numbers"
    CONSISTENCY = "consistency"
    CONFIDENCE_SANITY = "confidence_sanity"
    ABSTENTION = "abstention"


@dataclass(frozen=True)
class CheckResult:
    """What one check concluded, and about which claims."""

    name: CheckName
    passed: bool
    detail: str
    # Indices into the report's claims, so a caller can remove exactly the
    # claims that failed rather than guessing from the text.
    failed_claims: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "check": self.name.value,
            "passed": self.passed,
            "detail": self.detail,
            "failed_claims": list(self.failed_claims),
        }


@dataclass
class GateOutcome:
    """The report as published, and everything the gate did to it."""

    report: Report
    results: list[CheckResult] = field(default_factory=list)
    removed: int = 0
    repaired: bool = False
    initial_results: tuple[CheckResult, ...] = ()

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)

    @property
    def failed_checks(self) -> tuple[CheckName, ...]:
        return tuple(r.name for r in self.results if not r.passed)

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "removed_claims": self.removed,
            "repaired": self.repaired,
            "checks": [r.as_dict() for r in self.results],
            "initial_checks": [r.as_dict() for r in self.initial_results],
        }

    @property
    def notice(self) -> str | None:
        """The line SPEC.md Section 6.9 requires a stripped report to carry."""
        if not self.removed:
            return None
        if self.removed == 1:
            return "1 statement could not be verified and was removed."
        return f"{self.removed} statements could not be verified and were removed."


def check_coverage(report: Report, evidence: dict[str, EvidenceRecord]) -> CheckResult:
    """Check 1: every claim cites at least one id, and every id exists."""
    failed = []
    reasons = []
    for index, claim in enumerate(report.claims):
        if not claim.evidence_ids:
            failed.append(index)
            reasons.append(f"claim {index} cites nothing")
            continue
        missing = [ref for ref in claim.evidence_ids if ref not in evidence]
        if missing:
            failed.append(index)
            reasons.append(f"claim {index} cites unknown {', '.join(missing)}")
    if failed:
        return CheckResult(CheckName.COVERAGE, False, "; ".join(reasons[:4]), tuple(failed))
    return CheckResult(
        CheckName.COVERAGE, True, f"all {len(report.claims)} claim(s) cite known evidence"
    )


def check_re_execution(
    report: Report,
    evidence: dict[str, EvidenceRecord],
    rerun: dict[str, EvidenceRecord] | None = None,
) -> CheckResult:
    """Check 2: each cited record re-runs to the same hash.

    `rerun` is what the backend returned when the record's query was executed
    again. Passed in rather than executed here, so this stays a pure function
    and the caller owns the backend.

    **Absent `rerun` is not a pass.** A gate that treated "nothing was re-run"
    as "everything matched" would silently become a no-op the first time a
    caller forgot to re-run, and this is the check the others depend on. It
    reports honestly that it could not verify.

    **Live evidence is allowed to drift, bundle evidence is not.** SPEC.md
    Section 6.9 check 2 says a hash match, or for live data that may have
    shifted slightly, every cited number matching within tolerance. A live query
    re-run a minute later picks up a minute of new samples, so a hash comparison
    alone would fail every report produced by `make live`. A bundle is frozen, so
    a bundle record whose hash moved means the bundle changed underneath the run
    or the record was never real, and neither is something to wave through.
    """
    cited = report.cited_evidence
    if not cited:
        return CheckResult(CheckName.RE_EXECUTION, True, "nothing cited to re-run")
    if rerun is None:
        return CheckResult(
            CheckName.RE_EXECUTION,
            False,
            f"{len(cited)} citation(s) were not re-run, so none is verified",
            tuple(range(len(report.claims))),
        )

    mismatched = []
    drifted = 0
    for ref in cited:
        original = evidence.get(ref)
        fresh = rerun.get(ref)
        if original is None or fresh is None:
            mismatched.append(ref)
            continue
        if fresh.result_sha256 == original.result_sha256:
            continue
        if original.fingerprint.mode is BackendMode.LIVE and _numbers_survive_rerun(
            report, ref, fresh
        ):
            drifted += 1
            continue
        mismatched.append(ref)

    if mismatched:
        failed = tuple(
            index
            for index, claim in enumerate(report.claims)
            if any(ref in mismatched for ref in claim.evidence_ids)
        )
        return CheckResult(
            CheckName.RE_EXECUTION,
            False,
            f"{len(mismatched)} citation(s) did not re-run to the same result: "
            f"{', '.join(sorted(mismatched)[:3])}",
            failed,
        )
    if drifted:
        return CheckResult(
            CheckName.RE_EXECUTION,
            True,
            f"{len(cited) - drifted} citation(s) re-ran identically and {drifted} live "
            "citation(s) shifted while every cited number still matched",
        )
    return CheckResult(
        CheckName.RE_EXECUTION, True, f"all {len(cited)} citation(s) re-ran identically"
    )


def _numbers_survive_rerun(report: Report, ref: str, fresh: EvidenceRecord) -> bool:
    """Whether every number the report sourced from `ref` still holds.

    A record nothing quoted a number from cannot pass this way. Otherwise live
    mode would accept any drift at all in evidence used only as prose support,
    which is most of it, and check 2 would stop meaning anything the moment the
    backend was live.
    """
    quoted = [
        number for claim in report.claims for number in claim.numbers if number.evidence_id == ref
    ]
    if not quoted:
        return False
    for number in quoted:
        fact = fresh.fact_for(number.field)
        if fact is None or not _matches(fact, number.value):
            return False
    return True


def check_numbers(report: Report, evidence: dict[str, EvidenceRecord]) -> CheckResult:
    """Check 3: every number in a claim appears in the evidence it cites.

    The check that catches the most dangerous kind of fabrication, because a
    wrong number in a correct sentence is what an operator acts on. A claim
    saying payment's error rate reached 0.42 is only checkable if 0.42 is in the
    facts of the record it cites.

    Counts must match exactly and rates within one percent, per SPEC.md Section
    6.9. A count that is nearly right is wrong: there is no such thing as 4.02
    services.
    """
    failed = []
    reasons = []
    for index, claim in enumerate(report.claims):
        prose = claim.text
        if claim.at is not None:
            prose = prose.replace(claim.at.isoformat(), "")
        literals = [float(value.replace(",", "")) for value in NUMERIC_LITERAL.findall(prose)]
        if NUMBER_WORD.search(prose) or any(
            not any(
                abs(value - number.value) <= tolerance_for(number.unit, number.value)
                for number in claim.numbers
            )
            for value in literals
        ):
            failed.append(index)
            reasons.append(f"claim {index} has a number without structured provenance; use digits")
        for number in claim.numbers:
            if number.evidence_id not in claim.evidence_ids:
                failed.append(index)
                reasons.append(
                    f"claim {index} sources a number from {number.evidence_id}, "
                    "which the claim does not cite"
                )
                continue
            record = evidence.get(number.evidence_id)
            if record is None:
                failed.append(index)
                reasons.append(f"claim {index} sources a number from unknown evidence")
                continue
            fact = record.fact_for(number.field)
            if fact is None:
                failed.append(index)
                reasons.append(
                    f"claim {index} cites field {number.field!r}, which "
                    f"{number.evidence_id} does not report"
                )
                continue
            if fact.unit != number.unit or not _matches(fact, number.value):
                failed.append(index)
                reasons.append(
                    f"claim {index} states {number.value} for {number.field} where the "
                    f"evidence says {fact.value}"
                )
    if failed:
        return CheckResult(
            CheckName.NUMBERS, False, "; ".join(reasons[:4]), tuple(sorted(set(failed)))
        )
    counted = sum(len(claim.numbers) for claim in report.claims)
    return CheckResult(CheckName.NUMBERS, True, f"all {counted} cited number(s) match")


def check_consistency(
    report: Report, notebook: Notebook, evidence: dict[str, EvidenceRecord]
) -> CheckResult:
    """Check 4: the report agrees with the notebook and with the timestamps.

    Three parts, per SPEC.md Section 6.9. The named root cause must be the
    notebook's leading hypothesis, because a report naming anything else is
    contradicting the reasoning it came from. Timeline claims must be ordered by
    the evidence's own timestamps rather than by the order the prose lists them,
    since prose order is exactly what a fluent model gets wrong. And a blast
    radius must cite the graph.

    **What "the blast radius matches the graph query" can actually check here.**
    The set of affected services is not recomputed: doing that would need the
    gate to hold a graph client, and the gate calls nothing. What is checkable is
    the provenance. A blast radius claim has to cite a topology record, and any
    number in it is then verified against that record's facts by check 3. A blast
    radius asserted from prose alone fails, which is the failure this clause is
    for.
    """
    reasons = []
    failed: list[int] = []

    leader = notebook.leader
    if (
        report.root_cause_service is not None
        and leader is not None
        and report.root_cause_service != leader.service
    ):
        reasons.append(
            f"the report names {report.root_cause_service} but the notebook's "
            f"leading hypothesis is {leader.service}"
        )

    timeline = [
        (index, claim)
        for index, claim in enumerate(report.claims)
        if claim.claim_type is ClaimType.TIMELINE and claim.at is not None
    ]
    for (first_index, first), (_, second) in pairwise(timeline):
        if first.at is not None and second.at is not None and first.at > second.at:
            reasons.append(f"timeline claim {first_index} is dated after the one that follows it")
            failed.append(first_index)

    for index, claim in enumerate(report.claims):
        if claim.claim_type is not ClaimType.BLAST_RADIUS:
            continue
        kinds = {
            record.kind for ref in claim.evidence_ids if (record := evidence.get(ref)) is not None
        }
        if EvidenceKind.TOPOLOGY not in kinds:
            reasons.append(
                f"blast radius claim {index} cites no topology evidence, so it does "
                "not come from the graph"
            )
            failed.append(index)

    if reasons:
        return CheckResult(
            CheckName.CONSISTENCY, False, "; ".join(reasons[:4]), tuple(sorted(set(failed)))
        )
    return CheckResult(
        CheckName.CONSISTENCY, True, "the report agrees with the notebook and the timestamps"
    )


def signal_types(report: Report, evidence: dict[str, EvidenceRecord]) -> set[EvidenceKind]:
    """Which kinds of signal the report's citations actually rest on."""
    return {
        record.kind for ref in report.cited_evidence if (record := evidence.get(ref)) is not None
    }


def check_confidence_sanity(report: Report, evidence: dict[str, EvidenceRecord]) -> CheckResult:
    """Check 5: high confidence needs more than one kind of signal.

    A single metric series is enough to notice something and not enough to be
    sure of it. A confident report resting on one signal is the shape of every
    plausible wrong answer this system could produce, which is why the threshold
    is on the confidence rather than on the claim count.
    """
    if report.confidence is None:
        return CheckResult(CheckName.CONFIDENCE_SANITY, True, "no confidence stated")
    stated = report.confidence.as_probability
    if stated <= HIGH_CONFIDENCE:
        return CheckResult(
            CheckName.CONFIDENCE_SANITY,
            True,
            f"confidence {stated:.2f} is not above {HIGH_CONFIDENCE}",
        )
    kinds = signal_types(report, evidence)
    if len(kinds) >= MIN_SIGNAL_TYPES_FOR_HIGH_CONFIDENCE:
        return CheckResult(
            CheckName.CONFIDENCE_SANITY,
            True,
            f"confidence {stated:.2f} rests on {len(kinds)} signal types",
        )
    return CheckResult(
        CheckName.CONFIDENCE_SANITY,
        False,
        f"confidence {stated:.2f} rests on only "
        f"{len(kinds)} signal type ({', '.join(sorted(k.value for k in kinds)) or 'none'})",
    )


def check_abstention(report: Report, notebook: Notebook, min_support: int) -> CheckResult:
    """Check 6: below the support threshold, the report becomes insufficient evidence.

    The threshold is tuned on validation and lives in `config/thresholds.yaml`.
    Enforced here rather than asked of the reporter, because it is the check that
    stops a fluent model naming a service it has one weak reason to suspect.
    """
    if report.root_cause_service is None:
        return CheckResult(CheckName.ABSTENTION, True, "the report already names nobody")
    if not report.claims:
        return CheckResult(CheckName.ABSTENTION, False, "no verified claims support a root cause")
    leader = notebook.leader
    support = leader.support if leader else 0
    if support >= min_support:
        return CheckResult(
            CheckName.ABSTENTION, True, f"support {support} meets the threshold {min_support}"
        )
    return CheckResult(
        CheckName.ABSTENTION,
        False,
        f"support {support} is below the threshold {min_support}, so the report "
        "cannot name a root cause",
    )


def run_all_checks(
    report: Report,
    notebook: Notebook,
    evidence: dict[str, EvidenceRecord],
    rerun: dict[str, EvidenceRecord] | None,
    min_support: int,
) -> list[CheckResult]:
    """All six checks, in SPEC.md Section 6.9's order.

    One list in one place. An earlier version of `run_exit_gate` spelled this
    out three times for its three passes, which is exactly how two of them end
    up disagreeing about which checks exist.
    """
    return [
        check_coverage(report, evidence),
        check_re_execution(report, evidence, rerun),
        check_numbers(report, evidence),
        check_consistency(report, notebook, evidence),
        check_confidence_sanity(report, evidence),
        check_abstention(report, notebook, min_support),
    ]


def run_exit_gate(
    report: Report,
    notebook: Notebook,
    evidence: dict[str, EvidenceRecord],
    rerun: dict[str, EvidenceRecord] | None = None,
    min_support: int = 2,
    repair: bool = True,
) -> GateOutcome:
    """Run all six checks, repair once, then remove what still fails.

    Repair here means removing the offending claims and running the checks
    again, not asking a model to try harder: the gate cannot call a model, and a
    claim that failed verification is not made true by rewording. A caller that
    wants a rewrite reads `failed_checks` and asks for one before publishing.

    `repair=False` inspects without changing anything, which is what the eval
    harness wants when it is measuring how often a report needed repairing.
    """
    results = run_all_checks(report, notebook, evidence, rerun, min_support)
    offending, abstain = _what_to_repair(results)
    if all(result.passed for result in results):
        return GateOutcome(report, results, 0, False)
    if not repair:
        # Reports what is wrong and changes nothing. An earlier version stripped
        # the claims here anyway and then published the stripped report as
        # passing, which is the one thing this argument exists to prevent.
        return GateOutcome(report, results, 0, False)

    working = report
    leader = notebook.leader
    if (
        working.root_cause_service is not None
        and leader is not None
        and working.root_cause_service != leader.service
    ):
        # The prose may repeat the conflicting conclusion, so it must not survive either.
        offending.update(range(len(working.claims)))
        abstain = True
    if abstain:
        # Converted to insufficient evidence, keeping the claims: the ranked
        # candidates and what was checked are exactly what makes an abstention
        # useful rather than a shrug.
        working = working.model_copy(update={"root_cause_service": None, "confidence": None})
    kept = tuple(claim for index, claim in enumerate(working.claims) if index not in offending)
    removed = len(working.claims) - len(kept)
    working = working.model_copy(update={"claims": kept})

    if not working.claims:
        working = working.model_copy(update={"root_cause_service": None, "confidence": None})
    if not check_confidence_sanity(working, evidence).passed:
        working = working.model_copy(update={"confidence": Confidence.MEDIUM})
    checked = run_all_checks(working, notebook, evidence, rerun, min_support)
    if not all(item.passed for item in checked):
        # A failed repair cannot publish a claim merely because the repair allowance ended.
        removed = len(report.claims)
        working = working.model_copy(
            update={
                "root_cause_service": None,
                "confidence": None,
                "claims": (),
                "notes": (*working.notes, "Verification failed; insufficient evidence to publish."),
            }
        )
        checked = run_all_checks(working, notebook, evidence, rerun, min_support)
    return GateOutcome(working, checked, removed, True, tuple(results))


def _what_to_repair(results: list[CheckResult]) -> tuple[set[int], bool]:
    """Which claims to remove, and whether to abstain.

    Abstention is applied rather than repaired: it does not say a claim is
    unverifiable, it says the report may not name a root cause at all, so
    removing claims would not address it.
    """
    offending = {index for result in results if not result.passed for index in result.failed_claims}
    abstain = any(r.name is CheckName.ABSTENTION and not r.passed for r in results)
    return offending, abstain


def unsupported_claim_count(report: Report, evidence: dict[str, EvidenceRecord]) -> int:
    """How many claims would be removed, without removing them.

    Useful to a reporter deciding whether to rewrite before submitting.
    """
    return len(check_coverage(report, evidence).failed_claims)
