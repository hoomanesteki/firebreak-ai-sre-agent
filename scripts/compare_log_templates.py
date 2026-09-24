"""Decide ADR-0006: the deterministic masker against Drain, measured.

SPEC.md Section 6.5 says log lines are grouped into templates with a simple
deterministic masker, that Drain is the stronger option, and that ADR-0006
decides between them on a comparison against validation data. This is that
comparison.

**How the corpus is built.** Lines are generated here from the same
templates `firebreak.lab.synthetic` writes into bundles, but generated
directly rather than read back out of one, so each line keeps the id of the
template that produced it. That label is the ground truth this scores
against, and it never touches a bundle: a template id written into recorded
evidence would be a hint the agent could read.

**What is measured**, three things, because they answer different questions:

1. **Purity.** Does a cluster hold lines from exactly one true template, or
   has it merged two different events into one signature. A merge is the
   expensive error: it hides a new failure mode inside an existing count.
2. **Fragmentation.** Is one true template split across several clusters,
   which inflates the count of distinct signatures and buries a real spike.
3. **Stability.** Feed the same corpus in a different order and ask whether
   the clustering is the same. This is the property the masker's docstring
   claims decides the matter, and it is asserted here rather than assumed.

Run with `make compare-log-templates`.
"""

from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig

from _reporting import describe_path
from firebreak.lab.synthetic import (
    DEGRADED_ERROR_TEMPLATES,
    DEGRADED_WARN_TEMPLATES,
    HEALTHY_LOG_TEMPLATES,
    LOG_ROUTES,
    _fill_log_template,
)
from firebreak.triage.log_templates import mask_log_line

REPO_ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = REPO_ROOT / "reports" / "triage" / "log_template_comparison.json"

# Eight services, matching a bundle's usual breadth, so a template that
# embeds the service name produces the same spread it does in practice.
SERVICES = (
    "frontend",
    "cart",
    "checkout",
    "payment",
    "product-catalog",
    "recommendation",
    "shipping",
    "email",
)

LINES_PER_EVENT = 6
SEED = 7

# Shuffle seeds for the stability check. Three different orders, because one
# reordering that happens to produce the same answer proves nothing.
SHUFFLE_SEEDS = (11, 12, 13)


@dataclass(frozen=True)
class Labelled:
    """One generated line, with the template it came from."""

    body: str
    template_id: str


def build_corpus() -> list[Labelled]:
    """A labelled corpus from the same templates the fixtures use.

    **What counts as one true event** is a judgement that decides the
    result, so it is stated once here rather than left implicit:

        An event is its template plus every entity valued field it
        mentions. Numbers, durations, addresses and ids are noise.

    Entity fields are the service, the peer it called, and the route. Two
    lines differing only in a request id are the same event; two lines
    differing in which upstream failed are not, because "checkout cannot
    reach payment" and "checkout cannot reach email" send an operator to
    different places.

    Getting this rule wrong is not a small matter, and it was wrong twice
    while writing this. Labelling by template and service alone scored the
    masker as fragmenting 48 events, and then 16, when in both cases it was
    drawing a distinction the label had discarded.

    Status code is deliberately not an entity field, because neither
    algorithm can see it: it sits in the body as a bare number and both mask
    it away. That is a finding rather than a scoring choice, and ADR-0006
    reports it.
    """
    rng = random.Random(SEED)
    pools = {
        "healthy": HEALTHY_LOG_TEMPLATES,
        "error": DEGRADED_ERROR_TEMPLATES,
        "warn": DEGRADED_WARN_TEMPLATES,
    }
    corpus: list[Labelled] = []
    for pool_name, templates in pools.items():
        for index, template in enumerate(templates):
            routes = LOG_ROUTES if "{route}" in template else (None,)
            for service in SERVICES:
                peers_for = [other for other in SERVICES if other != service]
                peer_choices = peers_for if "{peer}" in template else [None]
                for route in routes:
                    for peer in peer_choices:
                        for _ in range(LINES_PER_EVENT):
                            corpus.append(
                                Labelled(
                                    body=_fill_log_template(
                                        rng,
                                        template,
                                        service,
                                        peers_for,
                                        peer=peer,
                                        route=route,
                                    ),
                                    template_id=(
                                        f"{pool_name}:{index}:{service}:"
                                        f"{route or '-'}:{peer or '-'}"
                                    ),
                                )
                            )
    return corpus


def cluster_with_masker(bodies: list[str]) -> list[str]:
    """Cluster by masking. Stateless, so order cannot matter."""
    return [mask_log_line(body) for body in bodies]


def cluster_with_drain(bodies: list[str]) -> list[str]:
    """Cluster with Drain, in its default configuration.

    A fresh miner per call, because Drain learns: reusing one across the
    stability runs would make the second run's answer depend on the first
    run's corpus, which is a different and much worse property than the one
    being measured.
    """
    config = TemplateMinerConfig()
    config.drain_depth = 4
    config.drain_sim_th = 0.4
    miner = TemplateMiner(config=config)
    assignments = []
    for body in bodies:
        result = miner.add_log_message(body)
        assignments.append(str(result["cluster_id"]))
    return assignments


def score(assignments: list[str], truth: list[str]) -> dict[str, Any]:
    """Purity and fragmentation for one clustering."""
    by_cluster: dict[str, Counter[str]] = defaultdict(Counter)
    by_truth: dict[str, set[str]] = defaultdict(set)
    for cluster, true_id in zip(assignments, truth, strict=True):
        by_cluster[cluster][true_id] += 1
        by_truth[true_id].add(cluster)

    # Purity: of all lines, the share sitting in a cluster whose majority
    # label is their own. One minus this is the share of lines silently
    # filed under another event's signature.
    correct = sum(counts.most_common(1)[0][1] for counts in by_cluster.values())
    merged = [cluster for cluster, counts in by_cluster.items() if len(counts) > 1]
    split = [true_id for true_id, clusters in by_truth.items() if len(clusters) > 1]

    return {
        "lines": len(assignments),
        "true_templates": len(by_truth),
        "clusters_found": len(by_cluster),
        "purity": round(correct / len(assignments), 4) if assignments else 0.0,
        "merged_clusters": len(merged),
        "fragmented_templates": len(split),
        "worst_merge": max((len(counts) for counts in by_cluster.values()), default=0),
    }


def stability(cluster: Any, corpus: list[Labelled]) -> dict[str, Any]:
    """Does the clustering survive the lines arriving in a different order.

    The comparison is made per line rather than over the set of cluster
    names, since the names themselves are arbitrary. Two orderings agree if
    every pair of lines that shared a cluster in one ordering shares a
    cluster in the other.
    """
    bodies = [item.body for item in corpus]
    reference = cluster(bodies)
    reference_groups = _grouping(reference, bodies)

    disagreements = []
    for shuffle_seed in SHUFFLE_SEEDS:
        shuffled = list(corpus)
        random.Random(shuffle_seed).shuffle(shuffled)
        shuffled_bodies = [item.body for item in shuffled]
        assignments = cluster(shuffled_bodies)
        groups = _grouping(assignments, shuffled_bodies)
        differing = sum(1 for body, group in reference_groups.items() if groups.get(body) != group)
        disagreements.append(differing)

    return {
        "shuffles": len(SHUFFLE_SEEDS),
        "lines_assigned_differently": disagreements,
        "stable": all(count == 0 for count in disagreements),
    }


def _grouping(assignments: list[str], bodies: list[str]) -> dict[str, frozenset[str]]:
    """Map each line to the set of lines sharing its cluster.

    Comparing sets of co-clustered lines rather than cluster labels is what
    makes this independent of the arbitrary names each algorithm assigns.
    """
    members: dict[str, list[str]] = defaultdict(list)
    for cluster, body in zip(assignments, bodies, strict=True):
        members[cluster].append(body)
    return {
        body: frozenset(members[cluster]) for cluster, body in zip(assignments, bodies, strict=True)
    }


def main() -> int:
    corpus = build_corpus()
    bodies = [item.body for item in corpus]
    truth = [item.template_id for item in corpus]

    masker = score(cluster_with_masker(bodies), truth)
    drain = score(cluster_with_drain(bodies), truth)

    # The realistic condition. `top_error_signatures` filters to error and
    # fatal severities before clustering, so a healthy 200 line and a failing
    # 503 line never meet in the same call. Scoring the whole corpus together
    # charges both algorithms for a merge the system never performs, which
    # would be measuring something nobody runs.
    error_corpus = [item for item in corpus if item.template_id.startswith("error:")]
    error_bodies = [item.body for item in error_corpus]
    error_truth = [item.template_id for item in error_corpus]
    masker_errors = score(cluster_with_masker(error_bodies), error_truth)
    drain_errors = score(cluster_with_drain(error_bodies), error_truth)

    report = {
        "what": "Deterministic masker against Drain, for ADR-0006",
        "why": (
            "SPEC.md Section 6.5 names Drain as the stronger option and requires "
            "ADR-0006 to decide on a comparison rather than on preference."
        ),
        "data_source": (
            "lines generated from the same templates src/firebreak/lab/synthetic.py "
            "writes into bundles, labelled with the template that produced them"
        ),
        "caveat": (
            "Synthetic log lines. Real service logs are more varied, and Drain's "
            "advantage grows with variety, so this understates it. Re-run on recorded "
            "incidents before treating the decision as settled."
        ),
        "corpus": {"lines": len(bodies), "true_templates": len(set(truth))},
        "masker": {
            **masker,
            "errors_only": masker_errors,
            "stability": stability(cluster_with_masker, corpus),
        },
        "drain": {
            **drain,
            "errors_only": drain_errors,
            "stability": stability(cluster_with_drain, corpus),
        },
    }

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    for name in ("masker", "drain"):
        entry = report[name]
        assert isinstance(entry, dict)
        print(
            f"{name:8s} clusters={entry['clusters_found']:4d} "
            f"(true {entry['true_templates']}) purity={entry['purity']:.4f} "
            f"merged={entry['merged_clusters']:3d} fragmented={entry['fragmented_templates']:3d} "
            f"stable={entry['stability']['stable']}"
        )
        errors = entry["errors_only"]
        print(
            f"{'':8s}   errors only: clusters={errors['clusters_found']:4d} "
            f"(true {errors['true_templates']}) purity={errors['purity']:.4f} "
            f"merged={errors['merged_clusters']:3d}"
        )
    print(f"\nwrote {describe_path(REPORT_PATH)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
