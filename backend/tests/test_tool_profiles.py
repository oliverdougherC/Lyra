"""Phase 4 registry-profile invariants over annotated tool definitions."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from backend.llm import tool_profiles, tools
from backend.tools.result import ToolResult, success


def _handler(**kwargs: object) -> ToolResult:
    return success(**kwargs)


def _tool(name: str, handler: Callable[..., ToolResult] = _handler) -> tools.ToolDefinition:
    return tools.ToolDefinition(
        name=name,
        description=f"{name} description",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=handler,
    )


def _annotated_tools() -> tuple[tool_profiles.AnnotatedToolDefinition, ...]:
    return (
        tool_profiles.annotate_tool_definition(
            _tool("cas_evaluate"), capability="compute", effect="pure", trust="computed"
        ),
        tool_profiles.annotate_tool_definition(
            _tool("search_web"), capability="web_read", effect="network_read", trust="public_web"
        ),
        tool_profiles.annotate_tool_definition(
            _tool("fetch_source"),
            capability="web_read",
            effect="network_read",
            trust="public_web",
        ),
        tool_profiles.annotate_tool_definition(
            _tool("list_workspace"),
            capability="workspace_read",
            effect="filesystem_read",
            trust="workspace",
        ),
        tool_profiles.annotate_tool_definition(
            _tool("propose_workspace_change"),
            capability="change_proposal",
            effect="database_proposal",
            trust="database",
        ),
        tool_profiles.annotate_tool_definition(
            _tool("propose_verification_command"),
            capability="command_proposal",
            effect="database_proposal",
            trust="database",
        ),
        tool_profiles.annotate_tool_definition(
            _tool("save_source"),
            capability="source_proposal",
            effect="database_proposal",
            trust="database",
        ),
        tool_profiles.annotate_tool_definition(
            _tool("save_profile"),
            capability="profile_proposal",
            effect="database_proposal",
            trust="database",
        ),
        tool_profiles.annotate_tool_definition(
            _tool("save_draft"),
            capability="draft_proposal",
            effect="database_proposal",
            trust="database",
        ),
        tool_profiles.annotate_tool_definition(
            _tool("save_comment"),
            capability="comment_proposal",
            effect="database_proposal",
            trust="database",
        ),
    )


def test_decorator_wrapper_returns_annotated_definition() -> None:
    annotated = tool_profiles.annotate_tool(capability="compute", effect="pure", trust="computed")(
        _tool("echo")
    )

    assert annotated.name == "echo"
    assert annotated.capability == "compute"
    assert annotated.effect == "pure"
    assert annotated.trust == "computed"


def test_declared_profiles_are_explicit_and_complete() -> None:
    assert tool_profiles.declared_profiles() == (
        "compute",
        "research",
        "code",
        "command",
        "writer",
    )

    profiles = tool_profiles.build_declared_profiles(_annotated_tools())

    assert tuple(profiles) == tool_profiles.declared_profiles()


def test_profile_matrix_enforces_all_phase_four_invariants() -> None:
    profiles = tool_profiles.build_declared_profiles(_annotated_tools())

    assert set(profiles["compute"].names()) == {"cas_evaluate"}
    assert set(profiles["research"].names()) == {
        "cas_evaluate",
        "search_web",
        "fetch_source",
        "save_source",
        "save_profile",
    }
    assert set(profiles["code"].names()) == {
        "cas_evaluate",
        "list_workspace",
        "propose_workspace_change",
    }
    assert set(profiles["command"].names()) == {"cas_evaluate", "propose_verification_command"}
    assert set(profiles["writer"].names()) == {
        "cas_evaluate",
        "search_web",
        "fetch_source",
        "save_source",
        "save_draft",
        "save_comment",
    }

    for annotated in profiles["research"].definitions:
        assert annotated.capability not in {"workspace_read", "change_proposal", "command_proposal"}
    for annotated in profiles["code"].definitions:
        assert annotated.capability not in {"web_read", "command_proposal"}
    for annotated in profiles["command"].definitions:
        assert annotated.capability not in {"web_read", "workspace_read", "change_proposal"}
    for annotated in profiles["writer"].definitions:
        assert annotated.capability != "workspace_read"
        assert annotated.capability != "change_proposal"
        assert annotated.capability != "command_proposal"


def test_rebuilding_profile_revokes_removed_capabilities() -> None:
    annotated = _annotated_tools()

    research = tool_profiles.build_tool_profile("research", annotated)
    compute = tool_profiles.build_tool_profile("compute", annotated)

    assert research.get("search_web") is not None
    assert compute.get("search_web") is None
    assert set(compute.names()) == {"cas_evaluate"}


def test_duplicate_tool_names_are_rejected_even_before_filtering() -> None:
    duplicate = (
        tool_profiles.annotate_tool_definition(
            _tool("echo"), capability="compute", effect="pure", trust="computed"
        ),
        tool_profiles.annotate_tool_definition(
            _tool("echo"), capability="web_read", effect="network_read", trust="public_web"
        ),
    )

    with pytest.raises(ValueError, match="Duplicate tool definition: echo"):
        tool_profiles.build_tool_profile("research", duplicate)


@pytest.mark.parametrize(
    ("capability", "effect", "trust"),
    [
        ("web_read", "pure", "public_web"),
        ("workspace_read", "network_read", "workspace"),
        ("command_proposal", "pure", "database"),
        ("compute", "database_proposal", "computed"),
    ],
)
def test_forbidden_capability_effect_combinations_are_rejected(
    capability: tool_profiles.Capability,
    effect: tool_profiles.Effect,
    trust: tool_profiles.Trust,
) -> None:
    with pytest.raises(ValueError, match="forbidden metadata"):
        tool_profiles.annotate_tool_definition(
            _tool("bad_tool"), capability=capability, effect=effect, trust=trust
        )


@pytest.mark.parametrize(
    "name",
    ["apply_workspace_change", "execute_command", "do-apply-now", "reapply", "executecommand"],
)
def test_apply_and_execute_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="forbidden"):
        tool_profiles.annotate_tool_definition(
            _tool(name), capability="change_proposal", effect="database_proposal", trust="database"
        )


def test_profiles_expose_no_host_commit_effects() -> None:
    profiles = tool_profiles.build_declared_profiles(_annotated_tools())

    all_effects = {
        annotated.effect for profile in profiles.values() for annotated in profile.definitions
    }
    all_names = {
        annotated.name for profile in profiles.values() for annotated in profile.definitions
    }

    assert all_effects <= {"pure", "network_read", "filesystem_read", "database_proposal"}
    assert "apply_workspace_change" not in all_names
    assert "execute_command" not in all_names
    assert "propose_workspace_change" in all_names
    assert "propose_verification_command" in all_names


def test_profile_registry_is_immutable() -> None:
    profile = tool_profiles.build_tool_profile("research", _annotated_tools())

    with pytest.raises(TypeError):
        profile.registry["new"] = tool_profiles.annotate_tool_definition(
            _tool("new"), capability="compute", effect="pure", trust="computed"
        )
