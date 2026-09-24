"""The hand written knowledge about the system: services, teams, runbooks.

Everything here is authored by a person rather than observed from telemetry.
Telemetry knows that `checkout` called `payment` nine hundred times in the
last minute. It does not know who to wake up when `payment` breaks at three
in the morning, which is the question an incident actually turns on.

**Why this is validated rather than read loosely.** These files are the only
input to the graph that nothing else checks. A typo in a service name
produces a `Team` owning a service that does not exist, and the failure
surfaces much later as a runbook that no query ever returns. Validating on
load turns that into an error with a filename attached.

**Leakage.** SPEC.md Section 8.4 rule 6 requires that knowledge files are
written before scenarios are recorded and reviewed for hints about specific
scenarios. A runbook saying "payment errors at fifty percent mean the
payment failure flag is on" would hand the agent an answer it is supposed to
derive, and the eval number afterwards would be fiction. `scripts/
check_leakage.py` enforces the mechanical part of that rule; the judgement
part is a review, and `docs/phase-reports/P04.md` records that it happened.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

REPO_ROOT = Path(__file__).resolve().parents[3]
KNOWLEDGE_DIR = REPO_ROOT / "knowledge"
SERVICES_FILE = "services.yaml"
TEAMS_FILE = "teams.yaml"
REMEDIATIONS_FILE = "remediations.yaml"
RUNBOOKS_DIRNAME = "runbooks"

# Runbook frontmatter is a YAML block fenced by --- at the very start of the
# file, the convention every static site generator uses and therefore the one
# a person editing these will expect.
FRONTMATTER = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*\n(?P<content>.*)\Z", re.DOTALL)

IDENTIFIER = r"^[a-z0-9]+(-[a-z0-9]+)*$"


class KnowledgeError(Exception):
    """A knowledge file is missing, malformed, or refers to something absent."""


class Tier(StrEnum):
    """How close to the customer a service sits.

    Used to explain impact rather than to rank suspects. An edge service
    failing is visible to a user immediately; a supporting one failing may
    not be visible at all until something else times out.
    """

    EDGE = "edge"
    CORE = "core"
    SUPPORTING = "supporting"
    INFRASTRUCTURE = "infrastructure"


class Access(StrEnum):
    READS = "reads"
    WRITES = "writes"
    READS_WRITES = "reads_writes"


class QueueRole(StrEnum):
    PRODUCES = "produces"
    CONSUMES = "consumes"


class RemediationKind(StrEnum):
    """The only actions Firebreak may ever propose.

    An allowlist, not a starting point. SPEC.md Section 7 puts remediation
    behind an approval gate, and an open ended action space would make that
    gate the only thing standing between a language model and a production
    change. Three kinds are enough for this system and each is either
    trivially reversible or is not an action at all.
    """

    DISABLE_FEATURE_FLAG = "disable_feature_flag"
    RESTART_SERVICE = "restart_service"
    PAGE_OWNING_TEAM = "page_owning_team"


class DatastoreLink(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    kind: str
    access: Access


class QueueLink(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    role: QueueRole


class EndpointSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    route: str


class ServiceSpec(BaseModel):
    """One application service, as a person describes it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=IDENTIFIER)
    language: str
    tier: Tier
    description: str
    owning_team: str
    container: str
    datastores: tuple[DatastoreLink, ...] = ()
    queues: tuple[QueueLink, ...] = ()
    endpoints: tuple[EndpointSpec, ...] = ()
    runbooks: tuple[str, ...] = ()


class TeamSpec(BaseModel):
    """A team, and how to reach it out of hours."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    slug: str = Field(pattern=IDENTIFIER)
    description: str
    escalation: str
    services: tuple[str, ...]

    @model_validator(mode="after")
    def _owns_something(self) -> TeamSpec:
        if not self.services:
            raise ValueError(f"team {self.name!r} owns no services")
        return self


class RemediationSpec(BaseModel):
    """One allowed action, with what has to be true before proposing it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=IDENTIFIER)
    kind: RemediationKind
    title: str
    description: str
    preconditions: tuple[str, ...] = ()
    blast_radius: str
    reversible: bool
    requires_approval: bool
    runbooks: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _irreversible_actions_need_approval(self) -> RemediationSpec:
        """An action nobody can undo may not be taken without a person.

        Asserted in the model rather than trusted to whoever edits the YAML,
        because this is the single rule in the file whose violation is not
        recoverable.
        """
        if not self.reversible and not self.requires_approval:
            raise ValueError(f"remediation {self.id!r} is irreversible and must require approval")
        return self


class RunbookSpec(BaseModel):
    """A runbook's frontmatter plus its body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=IDENTIFIER)
    title: str
    covers: tuple[str, ...]
    symptoms: tuple[str, ...] = ()
    updated: str
    path: str
    body: str = ""


class Knowledge(BaseModel):
    """Everything in `knowledge/`, validated and cross referenced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    services: tuple[ServiceSpec, ...]
    teams: tuple[TeamSpec, ...]
    remediations: tuple[RemediationSpec, ...]
    runbooks: tuple[RunbookSpec, ...]

    @property
    def service_names(self) -> set[str]:
        return {service.name for service in self.services}

    def service(self, name: str) -> ServiceSpec | None:
        return next((s for s in self.services if s.name == name), None)

    def team_for(self, service: str) -> TeamSpec | None:
        return next((t for t in self.teams if service in t.services), None)

    def runbooks_for(self, service: str) -> tuple[RunbookSpec, ...]:
        return tuple(r for r in self.runbooks if service in r.covers)

    @model_validator(mode="after")
    def _cross_references_resolve(self) -> Knowledge:
        """Every name pointing at something must point at something real.

        This is the check that earns the module its existence. Each of these
        failures is silent at load time and expensive later: a runbook that
        covers a misspelled service is a runbook no query returns, and an
        ownership gap is a page that goes nowhere at three in the morning.
        """
        names = self.service_names
        problems: list[str] = []

        runbook_ids = {runbook.id for runbook in self.runbooks}
        team_names = {team.name for team in self.teams}

        for service in self.services:
            if service.owning_team not in team_names:
                problems.append(
                    f"service {service.name!r} is owned by unknown team {service.owning_team!r}"
                )
            for cited in service.runbooks:
                if cited not in runbook_ids:
                    problems.append(f"service {service.name!r} cites unknown runbook {cited!r}")

        for team in self.teams:
            for owned in team.services:
                if owned not in names:
                    problems.append(f"team {team.name!r} owns unknown service {owned!r}")

        for runbook in self.runbooks:
            for covered in runbook.covers:
                if covered not in names:
                    problems.append(f"runbook {runbook.id!r} covers unknown service {covered!r}")

        for remediation in self.remediations:
            for cited in remediation.runbooks:
                if cited not in runbook_ids:
                    problems.append(
                        f"remediation {remediation.id!r} cites unknown runbook {cited!r}"
                    )

        # Ownership must be total and unambiguous. A service owned twice is
        # a page sent to two teams who each assume the other has it.
        owners: dict[str, list[str]] = {}
        for team in self.teams:
            for owned in team.services:
                owners.setdefault(owned, []).append(team.name)
        for name in sorted(names):
            holders = owners.get(name, [])
            if not holders:
                problems.append(f"service {name!r} is owned by no team")
            elif len(holders) > 1:
                problems.append(f"service {name!r} is owned by {len(holders)} teams: {holders}")

        # The fallback has to exist, or an incident with no safe automated
        # action would have no proposal at all rather than "page a human".
        if not any(r.kind is RemediationKind.PAGE_OWNING_TEAM for r in self.remediations):
            problems.append("no page_owning_team remediation, so there is no fallback action")

        if problems:
            raise ValueError("; ".join(problems))
        return self


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise KnowledgeError(f"cannot read {path.name}: {error}") from error
    except yaml.YAMLError as error:
        raise KnowledgeError(f"{path.name} is not valid YAML: {error}") from error


def _expect_list(value: Any, path: Path, key: str) -> list[Any]:
    if isinstance(value, dict) and key in value:
        value = value[key]
    if not isinstance(value, list):
        raise KnowledgeError(f"{path.name} must contain a list of {key}")
    return value


def _display_path(path: Path) -> str:
    """A repository relative path when possible, absolute otherwise.

    `load_knowledge` takes any directory, so a runbook is not guaranteed to
    live under the repository: a deployment can point at its own knowledge
    directory, and the tests use a temporary one. `relative_to` raises in
    that case rather than returning the absolute path, which turned a
    perfectly valid runbook into a validation error.
    """
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def parse_runbook(path: Path) -> RunbookSpec:
    """Read one runbook's frontmatter and body.

    The body is kept because `runbook_search` returns quoted sections, and a
    citation to a runbook that does not include its text is not a citation.
    """
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    if match is None:
        raise KnowledgeError(f"{path.name} has no YAML frontmatter block")
    try:
        header = yaml.safe_load(match.group("body"))
    except yaml.YAMLError as error:
        raise KnowledgeError(f"{path.name} has invalid frontmatter: {error}") from error
    if not isinstance(header, dict):
        raise KnowledgeError(f"{path.name} frontmatter is not a mapping")
    try:
        return RunbookSpec.model_validate(
            {**header, "path": _display_path(path), "body": match.group("content")}
        )
    except ValueError as error:
        raise KnowledgeError(f"{path.name} frontmatter is invalid: {error}") from error


def load_knowledge(directory: Path | None = None) -> Knowledge:
    """Read and cross check every knowledge file.

    Raises `KnowledgeError` naming the file, rather than a pydantic traceback
    naming a field, because the person who has to fix it is editing YAML and
    not reading this module.
    """
    root = directory or KNOWLEDGE_DIR
    if not root.is_dir():
        raise KnowledgeError(f"knowledge directory {root} does not exist")

    services_path = root / SERVICES_FILE
    teams_path = root / TEAMS_FILE
    remediations_path = root / REMEDIATIONS_FILE
    runbooks_dir = root / RUNBOOKS_DIRNAME

    services = _expect_list(_read_yaml(services_path), services_path, "services")
    teams = _expect_list(_read_yaml(teams_path), teams_path, "teams")
    remediations = _expect_list(_read_yaml(remediations_path), remediations_path, "remediations")

    if not runbooks_dir.is_dir():
        raise KnowledgeError(f"runbooks directory {runbooks_dir} does not exist")
    # Sorted, so the graph load and its checksum do not depend on the order
    # the filesystem happens to return names in.
    runbooks = [parse_runbook(path) for path in sorted(runbooks_dir.glob("*.md"))]

    try:
        return Knowledge.model_validate(
            {
                "services": services,
                "teams": teams,
                "remediations": remediations,
                "runbooks": runbooks,
            }
        )
    except ValueError as error:
        raise KnowledgeError(f"knowledge files do not agree with each other: {error}") from error
