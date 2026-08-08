"""Phase 4 tool metadata and immutable profile composition.

This module wraps existing ``tools.ToolDefinition`` instances with explicit metadata
so the shared loop can move from one global registry to contextual profiles without
changing ``tools.ToolDefinition`` yet.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from backend.llm.tools import ToolDefinition

type Capability = Literal[
    "compute",
    "web_read",
    "workspace_read",
    "change_proposal",
    "command_proposal",
    "source_proposal",
    "profile_proposal",
    "draft_proposal",
    "comment_proposal",
]
type Effect = Literal["pure", "network_read", "filesystem_read", "database_proposal"]
type Trust = Literal["computed", "public_web", "workspace", "database"]
type ProfileName = Literal["compute", "research", "code", "command", "writer"]

PROFILE_COMPUTE: ProfileName = "compute"
PROFILE_RESEARCH: ProfileName = "research"
PROFILE_CODE: ProfileName = "code"
PROFILE_COMMAND: ProfileName = "command"
PROFILE_WRITER: ProfileName = "writer"

DECLARED_PROFILES: tuple[ProfileName, ...] = (
    PROFILE_COMPUTE,
    PROFILE_RESEARCH,
    PROFILE_CODE,
    PROFILE_COMMAND,
    PROFILE_WRITER,
)

FORBIDDEN_NAME_TOKENS = frozenset({"apply", "execute"})
_ALLOWED_COMBINATIONS: Mapping[Capability, tuple[Effect, Trust]] = {
    "compute": ("pure", "computed"),
    "web_read": ("network_read", "public_web"),
    "workspace_read": ("filesystem_read", "workspace"),
    "change_proposal": ("database_proposal", "database"),
    "command_proposal": ("database_proposal", "database"),
    "source_proposal": ("database_proposal", "database"),
    "profile_proposal": ("database_proposal", "database"),
    "draft_proposal": ("database_proposal", "database"),
    "comment_proposal": ("database_proposal", "database"),
}
PROFILE_CAPABILITY_MATRIX: Mapping[ProfileName, frozenset[Capability]] = MappingProxyType(
    {
        PROFILE_COMPUTE: frozenset({"compute"}),
        PROFILE_RESEARCH: frozenset({"compute", "web_read", "source_proposal", "profile_proposal"}),
        PROFILE_CODE: frozenset({"compute", "workspace_read", "change_proposal"}),
        PROFILE_COMMAND: frozenset({"compute", "command_proposal"}),
        PROFILE_WRITER: frozenset(
            {
                "compute",
                "web_read",
                "source_proposal",
                "draft_proposal",
                "comment_proposal",
            }
        ),
    }
)


@dataclass(frozen=True, slots=True)
class AnnotatedToolDefinition:
    """A tool definition plus the capability/effect/trust metadata Phase 4 needs."""

    definition: ToolDefinition
    capability: Capability
    effect: Effect
    trust: Trust

    @property
    def name(self) -> str:
        return self.definition.name


@dataclass(frozen=True, slots=True)
class ToolProfile:
    """One immutable registry assembled for a specific capability profile."""

    name: ProfileName
    allowed_capabilities: frozenset[Capability]
    definitions: tuple[AnnotatedToolDefinition, ...]
    registry: Mapping[str, AnnotatedToolDefinition]

    def get(self, name: str) -> AnnotatedToolDefinition | None:
        return self.registry.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self.registry)


def annotate_tool_definition(
    definition: ToolDefinition, *, capability: Capability, effect: Effect, trust: Trust
) -> AnnotatedToolDefinition:
    annotated = AnnotatedToolDefinition(
        definition=definition, capability=capability, effect=effect, trust=trust
    )
    _validate_annotated_tool(annotated)
    return annotated


def annotate_tool(
    *, capability: Capability, effect: Effect, trust: Trust
) -> Callable[[ToolDefinition], AnnotatedToolDefinition]:
    """Decorator-style wrapper for existing ``ToolDefinition`` instances."""

    def wrap(definition: ToolDefinition) -> AnnotatedToolDefinition:
        return annotate_tool_definition(
            definition, capability=capability, effect=effect, trust=trust
        )

    return wrap


def build_tool_profile(
    profile: ProfileName, definitions: Iterable[AnnotatedToolDefinition]
) -> ToolProfile:
    """Filter annotated tools through the explicit Phase 4 capability matrix."""
    if profile not in PROFILE_CAPABILITY_MATRIX:
        raise ValueError(f"Unknown tool profile: {profile}")
    allowed = PROFILE_CAPABILITY_MATRIX[profile]
    selected: list[AnnotatedToolDefinition] = []
    seen: set[str] = set()
    for definition in definitions:
        _validate_annotated_tool(definition)
        if definition.name in seen:
            raise ValueError(f"Duplicate tool definition: {definition.name}")
        seen.add(definition.name)
        if definition.capability in allowed:
            selected.append(definition)
    registry = MappingProxyType({definition.name: definition for definition in selected})
    return ToolProfile(
        name=profile,
        allowed_capabilities=allowed,
        definitions=tuple(selected),
        registry=registry,
    )


def build_declared_profiles(
    definitions: Iterable[AnnotatedToolDefinition],
) -> Mapping[ProfileName, ToolProfile]:
    """Build every declared profile from the same annotated tool universe."""
    materialized = tuple(definitions)
    return MappingProxyType(
        {profile: build_tool_profile(profile, materialized) for profile in DECLARED_PROFILES}
    )


def declared_profiles() -> tuple[ProfileName, ...]:
    """Return the explicit set of supported Phase 4 profile names."""
    return DECLARED_PROFILES


def _validate_annotated_tool(definition: AnnotatedToolDefinition) -> None:
    expected = _ALLOWED_COMBINATIONS.get(definition.capability)
    if expected is None:
        raise ValueError(f"Unknown capability: {definition.capability}")
    if (definition.effect, definition.trust) != expected:
        raise ValueError(
            f"Tool {definition.name} declares forbidden metadata: "
            f"{definition.capability}/{definition.effect}/{definition.trust}"
        )
    _validate_name(definition.name)


def _validate_name(name: str) -> None:
    lowered = name.lower()
    if any(token in lowered for token in FORBIDDEN_NAME_TOKENS):
        raise ValueError(f"Tool names containing apply/execute are forbidden: {name}")
