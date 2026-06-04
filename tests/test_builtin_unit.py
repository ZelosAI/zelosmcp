"""Unit tests for the BuiltinServer's pure generator + helper functions.

Lifecycle (start/stop, in-memory streams, session manager) is covered by the
end-to-end app integration tests; here we focus on the rule-text rendering
and tool-handler dispatch logic that's easy to exercise without spawning a
session manager."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from zelosmcp.builtin import (
    NAME,
    _HANDLERS,
    _TOOLS,
    _classify_tool,
    _format_args,
    render_comprehensive_rule,
)


# Helpers used by several test classes.
def _tool(
    name: str,
    *,
    description: str = "desc",
    input_schema: dict | None = None,
    annotations: dict | None = None,
) -> dict:
    """Build a dict-shape Tool entry as `collect_backend_full_catalog`
    would emit it (post `model_dump(mode='json')`)."""
    out: dict = {"name": name, "description": description}
    if input_schema is not None:
        out["inputSchema"] = input_schema
    if annotations is not None:
        out["annotations"] = annotations
    return out


def _backend(tools: list[dict], **extra) -> dict:
    return {
        "transport": extra.get("transport", "stdio"),
        "running": True,
        "tools": tools,
        "prompts": extra.get("prompts", []),
        "resources": extra.get("resources", []),
        "resourceTemplates": extra.get("resourceTemplates", []),
    }


class TestFormatArgs:
    def test_no_schema(self):
        assert _format_args(None) == "()"

    def test_empty_dict(self):
        assert _format_args({}) == "()"

    def test_object_no_properties(self):
        # Object schema declared but with no `properties` field still
        # accepts an arbitrary object; flag with `(...)`.
        assert _format_args({"type": "object"}) == "(...)"

    def test_required_only(self):
        out = _format_args(
            {"type": "object", "properties": {"path": {}}, "required": ["path"]}
        )
        assert out == "(path)"

    def test_optional_only(self):
        out = _format_args({"type": "object", "properties": {"a": {}, "b": {}}})
        assert out == "(a?, b?)"

    def test_required_then_optional(self):
        out = _format_args(
            {
                "type": "object",
                "properties": {"path": {}, "head": {}, "tail": {}},
                "required": ["path"],
            }
        )
        assert out == "(path, head?, tail?)"

    def test_required_order_preserved(self):
        # `required` declared in non-properties order; required comes
        # first in the order declared, then any leftover properties.
        out = _format_args(
            {
                "type": "object",
                "properties": {"a": {}, "b": {}, "c": {}, "d": {}},
                "required": ["c", "a"],
            }
        )
        assert out == "(c, a, b?, d?)"

    def test_required_key_without_properties_entry(self):
        # Server declared a `required` name not in `properties`; we still
        # render it (don't drop it on the floor).
        out = _format_args(
            {"type": "object", "properties": {"b": {}}, "required": ["a"]}
        )
        assert out == "(a, b?)"

    def test_non_object_type(self):
        # Some schemas (rare) declare a top-level non-object type.
        out = _format_args({"type": "string"})
        assert out == "(string)"


class TestClassifyTool:
    def test_readonly_hint_wins_over_name(self):
        marker = _classify_tool(
            _tool(
                "create_thing",  # would be `mutates` by name
                annotations={"readOnlyHint": True},
            )
        )
        assert marker == "readonly"

    def test_destructive_hint_overrides_readonly(self):
        marker = _classify_tool(
            _tool(
                "list_things",
                annotations={"readOnlyHint": True, "destructiveHint": True},
            )
        )
        assert marker == "destructive"

    def test_destructive_hint_overrides_name(self):
        marker = _classify_tool(
            _tool("delete_pod", annotations={"destructiveHint": True})
        )
        assert marker == "destructive"

    @pytest.mark.parametrize(
        "name",
        [
            "create_container",
            "update_resource",
            "delete_pod",
            "remove_volume",
            "start_server",
            "stop_server",
            "restart_app",
            "run_pod",
            "push_image",
            "pull_image",
            "build_image",
            "write_file",
            "edit_file",
            "move_file",
            "configure_file_watcher",
            "reload_config",
            "set_project_path",
        ],
    )
    def test_mutating_name_prefix_without_annotations(self, name):
        assert _classify_tool(_tool(name)) == "mutates"

    def test_unknown_name_no_annotations(self):
        # No readOnlyHint, no destructive hint, no mutation prefix.
        assert _classify_tool(_tool("do_something")) == "?"

    def test_no_annotations_attr(self):
        # The annotations key is missing entirely.
        assert _classify_tool({"name": "list_things"}) == "?"


class TestRenderComprehensiveRule:
    def test_empty_catalog(self):
        out = render_comprehensive_rule({})
        assert "alwaysApply: true" in out
        assert "No user backends are currently loaded" in out
        assert "READ-ONLY" in out  # default access mode in the directive

    def test_only_builtin_in_catalog_treated_as_empty(self):
        # The renderer skips the builtin row by design — including its
        # own tools in the rule would tell the agent how to regenerate
        # itself (noisy and not useful).
        out = render_comprehensive_rule(
            {NAME: _backend([_tool("generate_cursor_rule")])}
        )
        assert "No user backends are currently loaded" in out

    def test_always_apply_frontmatter(self):
        out = render_comprehensive_rule(
            {"filesystem": _backend([_tool("read_text_file")])}
        )
        head = out.split("---\n", 2)[1]
        assert "alwaysApply: true" in head
        assert "globs:" not in head

    def test_scoped_frontmatter_with_globs(self):
        out = render_comprehensive_rule(
            {"filesystem": _backend([_tool("read_text_file")])},
            style="scoped",
            globs="**/*.py",
        )
        head = out.split("---\n", 2)[1]
        assert "alwaysApply: false" in head
        assert "globs: **/*.py" in head

    def test_scoped_defaults_globs(self):
        out = render_comprehensive_rule(
            {"filesystem": _backend([_tool("read_text_file")])},
            style="scoped",
        )
        assert "globs: **/*" in out

    def test_read_only_directive(self):
        out = render_comprehensive_rule(
            {"filesystem": _backend([_tool("read_text_file")])},
            access="read-only",
        )
        assert "Access mode: READ-ONLY" in out
        assert "Do not call" in out
        assert "Access mode: READ-WRITE" not in out

    def test_read_write_directive(self):
        out = render_comprehensive_rule(
            {"filesystem": _backend([_tool("read_text_file")])},
            access="read-write",
        )
        assert "Access mode: READ-WRITE" in out
        # The read-write directive (configs/assets/global.yaml) tells the
        # agent to confirm before destructive tools rather than confirm
        # every write. Assert the confirmation directive is present.
        assert "Confirm before any" in out
        assert "`[destructive]`" in out
        assert "Access mode: READ-ONLY" not in out

    def test_unknown_access_raises(self):
        with pytest.raises(ValueError, match="Unknown access"):
            render_comprehensive_rule({}, access="bogus")

    def test_unknown_format_raises(self):
        with pytest.raises(ValueError, match="Unknown format"):
            render_comprehensive_rule({}, fmt="bogus")

    def test_copilot_instructions_strips_frontmatter(self):
        """`fmt=copilot-instructions` returns the same body as cursor-mdc
        but without the YAML frontmatter wrapper. Suitable for
        `.github/copilot-instructions.md`."""
        catalog = {
            "fs": _backend(
                [
                    _tool(
                        "read_text_file",
                        annotations={"readOnlyHint": True},
                        input_schema={
                            "type": "object",
                            "properties": {"path": {}},
                            "required": ["path"],
                        },
                    ),
                ]
            )
        }
        mdc = render_comprehensive_rule(catalog, fmt="cursor-mdc")
        copi = render_comprehensive_rule(catalog, fmt="copilot-instructions")
        # No frontmatter in copilot-instructions output.
        assert not copi.startswith("---")
        assert "alwaysApply" not in copi
        assert "globs:" not in copi
        # Cursor MDC: strip the frontmatter and the body should match.
        assert mdc.startswith("---\n")
        _, _, mdc_after = mdc.partition("---\n")  # remove first ---
        _, _, mdc_body = mdc_after.partition("---\n")  # remove second ---
        assert mdc_body == copi
        # Body still carries the directive + per-tool entry.
        assert "# zelosMCP backend tool catalog" in copi
        assert "Access mode: READ-ONLY" in copi
        assert "`fs__read_text_file`" in copi

    def test_copilot_instructions_ignores_style_and_globs(self):
        """style/globs only affect frontmatter, which copilot-instructions
        omits. The body must be identical regardless of those args."""
        catalog = {"fs": _backend([_tool("read_text_file", annotations={"readOnlyHint": True})])}
        a = render_comprehensive_rule(
            catalog, fmt="copilot-instructions", style="always-apply"
        )
        b = render_comprehensive_rule(
            catalog,
            fmt="copilot-instructions",
            style="scoped",
            globs="**/*.py",
        )
        assert a == b

    def test_per_tool_entry_shape(self):
        out = render_comprehensive_rule(
            {
                "filesystem": _backend(
                    [
                        _tool(
                            "read_text_file",
                            description="Read complete contents.",
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "path": {},
                                    "head": {},
                                    "tail": {},
                                },
                                "required": ["path"],
                            },
                            annotations={"readOnlyHint": True},
                        )
                    ]
                )
            }
        )
        # Qualified name + arg summary + marker, then description on next line.
        assert (
            "- `filesystem__read_text_file` `(path, head?, tail?)` [readonly]"
            in out
        )
        assert "Read complete contents." in out

    def test_marker_taxonomy_renders(self):
        catalog = {
            "weirdly-named": _backend(
                [
                    _tool("get_status", annotations={"readOnlyHint": True}),
                    _tool("create_widget"),  # mutates by prefix
                    _tool(
                        "delete_widget", annotations={"destructiveHint": True}
                    ),
                    _tool("opaque_thing"),  # ?
                ]
            )
        }
        out = render_comprehensive_rule(catalog)
        assert "[readonly]" in out
        assert "[mutates]" in out
        assert "[destructive]" in out
        assert "[?]" in out

    def test_backend_with_no_tools(self):
        out = render_comprehensive_rule({"empty": _backend([])})
        assert "## `empty`" in out
        assert "_(no tools advertised)_" in out

    def test_tool_with_missing_description(self):
        out = render_comprehensive_rule(
            {"x": _backend([_tool("foo", description="")])}
        )
        assert "_(no description)_" in out

    def test_directive_present_even_when_backend_empty(self):
        # Empty user-backend catalog still gets the directive block
        # (so the agent knows access policy even before backends start).
        out = render_comprehensive_rule({}, access="read-only")
        # Empty body branch goes back early — verify directive appears
        # somewhere. We pick the description-suffix marker.
        assert "(read-only mode)" in out

    def test_tool_use_priority_default_includes_directive(self):
        """Default `tool_use=priority` injects the prefer-MCP-over-shell
        directive. Playbooks are now skills (not emitted in the rule)."""
        catalog = {
            "filesystem": _backend(
                [_tool("read_text_file", annotations={"readOnlyHint": True})]
            ),
            "pincher": _backend(
                [_tool("search", annotations={"readOnlyHint": True})]
            ),
        }
        out = render_comprehensive_rule(catalog)
        assert "## Tool-use priority" in out or "Tool-use priority" in out
        # Per-backend header still includes the prefer-over-shell hint.
        assert "Prefer these over equivalent shell commands." in out

    def test_priority_includes_path_translation_directive(self):
        out = render_comprehensive_rule(
            {
                "filesystem": _backend(
                    [_tool("read_text_file", annotations={"readOnlyHint": True})]
                )
            }
        )
        # The path-translation directive heading (configs/assets/global.yaml)
        # was simplified from "(MANDATORY)" to a plain section title; the
        # mandatory translation instruction lives in the body.
        assert "## Container path translation" in out
        assert "Translate host paths before every MCP call." in out
        assert "/user_data_ro/<repo>" in out
        assert "/Users/KMECHL/workspace" in out

    def test_per_backend_and_builtin_skills_render(self):
        out = render_comprehensive_rule(
            {
                "pincher": _backend(
                    [_tool("architecture", annotations={"readOnlyHint": True})]
                ),
                "filesystem": _backend(
                    [_tool("read_text_file", annotations={"readOnlyHint": True})]
                ),
            },
            skill_assets={
                "zelosmcp": [
                    SimpleNamespace(
                        slug="zelosmcp-onboarding",
                        description="Understand zelosMCP setup.",
                    )
                ],
                "pincher": [
                    SimpleNamespace(
                        slug="zelosmcp-pincher",
                        description="Codebase intelligence with pincher.",
                    )
                ],
            },
        )
        assert "## Built-in skills" in out
        assert "/zelosmcp-onboarding" in out
        assert "### Available skills" in out
        assert "/zelosmcp-pincher" in out

    def test_tool_use_available_omits_priority_sections(self):
        """`tool_use=available` strips every prefer-MCP phrasing."""
        catalog = {
            "filesystem": _backend(
                [_tool("read_text_file", annotations={"readOnlyHint": True})]
            ),
            "pincher": _backend(
                [_tool("search", annotations={"readOnlyHint": True})]
            ),
        }
        out = render_comprehensive_rule(catalog, tool_use="available")
        assert "Tool-use priority" not in out
        assert "Prefer these over shelling out" not in out
        assert "Prefer these over equivalent shell commands" not in out

    def test_unknown_tool_use_raises(self):
        with pytest.raises(ValueError, match="Unknown tool_use"):
            render_comprehensive_rule({}, tool_use="bogus")

    def test_full_default_set_renders_cleanly(self):
        """Smoke test: a default-zelosmcp.json-shaped catalog renders
        every backend as its own section with per-tool entries."""
        catalog = {
            "filesystem": _backend(
                [
                    _tool(
                        "read_text_file",
                        annotations={"readOnlyHint": True},
                        input_schema={
                            "type": "object",
                            "properties": {"path": {}},
                            "required": ["path"],
                        },
                    ),
                    _tool(
                        "edit_file",
                        annotations={"destructiveHint": True},
                        input_schema={
                            "type": "object",
                            "properties": {"path": {}, "edits": {}},
                            "required": ["path", "edits"],
                        },
                    ),
                ]
            ),
            "pincher": _backend(
                [
                    _tool("search", annotations={"readOnlyHint": True}),
                    _tool("index"),  # ambiguous mutability
                ]
            ),
            "docker": _backend(
                [
                    _tool("list_containers", annotations={"readOnlyHint": True}),
                    _tool("pull_image"),  # mutates by prefix
                ]
            ),
            "kubernetes": _backend(
                [
                    _tool("pods_list", annotations={"readOnlyHint": True}),
                    _tool("pods_delete", annotations={"destructiveHint": True}),
                ]
            ),
        }
        out = render_comprehensive_rule(catalog)
        for header in (
            "## `filesystem`",
            "## `pincher`",
            "## `docker`",
            "## `kubernetes`",
        ):
            assert header in out
        assert "## Mutability markers" in out
        assert "## Tool naming convention" in out
        assert "## Don't do this" in out
        # Pick one entry from each backend to spot-check qualification.
        assert "`filesystem__read_text_file`" in out
        assert "`kubernetes__pods_delete`" in out


class TestRenderComprehensiveRuleCompressed:
    """Integration tests for the compressed-backend rendering path
    inside ``render_comprehensive_rule``."""

    _MEDIUM = {"level": "medium", "scope": "aggregator"}
    _MAX = {"level": "max", "scope": "global"}

    def _cat(self, **kwargs) -> dict:
        """Build a minimal catalog. kwargs maps backend -> list[tool_name]."""
        return {
            name: _backend([_tool(t) for t in tools])
            for name, tools in kwargs.items()
        }

    def test_wrapper_trio_shown_for_compressed_backend(self):
        out = render_comprehensive_rule(
            self._cat(pincher=["architecture", "search"]),
            compressed_backends={"pincher": self._MEDIUM},
        )
        assert "pincher__get_tool_schema" in out
        assert "pincher__search_tools" in out
        assert "pincher__invoke_tool" in out

    def test_underlying_tools_not_qualified(self):
        """Underlying tool names in the sub-list must not be prefixed with
        `backend__` because they cannot be called directly when compressed."""
        # Use a synthetic tool name that doesn't appear in any global-block
        # example text so the assertion is unambiguous.
        out = render_comprehensive_rule(
            self._cat(pincher=["xyzzy_unique_tool"]),
            compressed_backends={"pincher": self._MEDIUM},
        )
        # Underlying tool listed as bare name in the sub-list
        assert "- `xyzzy_unique_tool`" in out
        # NOT as a qualified direct call — must not appear in the output at all
        assert "pincher__xyzzy_unique_tool" not in out

    def test_level_max_emits_single_list_tools(self):
        # Use tool_use=available to suppress the mandatory playbook which
        # mentions invoke_tool in its text; we want to isolate the per-backend
        # section behavior.
        out = render_comprehensive_rule(
            self._cat(pincher=["architecture"]),
            compressed_backends={"pincher": self._MAX},
            tool_use="available",
        )
        assert "pincher__list_tools" in out
        assert "pincher__get_tool_schema" not in out
        # invoke_tool may appear in sub-section noun but NOT as a wire-level bullet
        assert "- `pincher__invoke_tool`" not in out

    def test_compressed_rules_block_present_when_compressed_backend_exists(self):
        out = render_comprehensive_rule(
            self._cat(pincher=["architecture"]),
            tool_use="priority",
            compressed_backends={"pincher": self._MEDIUM},
        )
        assert "Compressed backends" in out

    def test_uncompressed_backend_unaffected(self):
        out = render_comprehensive_rule(
            self._cat(mybackend=["do_thing"]),
            compressed_backends={"other_backend": self._MEDIUM},
        )
        assert "mybackend__do_thing" in out
        assert "Compressed backends" not in out

    def test_no_compressed_backends_dict_behaves_normally(self):
        out = render_comprehensive_rule(
            self._cat(pincher=["architecture"]),
            compressed_backends=None,
        )
        assert "pincher__architecture" in out
        assert "Compressed backends" not in out


class TestToolRegistry:
    def test_eight_tools_registered(self):
        # Count history: 7 → 8 (`list_compressed_tools`) → 9 (`warm_up_
        # passthrough_auth`) → 8 again (warm_up dropped now that the
        # aggregator emits compression wrappers for passthrough backends
        # — first wrapper invocation drives OAuth on demand). If you add
        # a builtin, bump this number AND ship a handler in `_HANDLERS`.
        assert len(_TOOLS) == 8

    def test_handler_per_tool(self):
        names = [t.name for t in _TOOLS]
        for n in names:
            assert n in _HANDLERS, f"missing handler for {n}"

    def test_list_supported_backends_removed(self):
        names = {t.name for t in _TOOLS}
        assert "list_supported_backends" not in names
        assert "list_supported_backends" not in _HANDLERS

    def test_generate_cursor_rule_schema_includes_access(self):
        gen = next(t for t in _TOOLS if t.name == "generate_cursor_rule")
        props = gen.inputSchema["properties"]
        assert "access" in props
        assert props["access"]["enum"] == ["read-only", "read-write"]
        assert props["access"]["default"] == "read-only"

    def test_generate_cursor_rule_schema_includes_format(self):
        gen = next(t for t in _TOOLS if t.name == "generate_cursor_rule")
        props = gen.inputSchema["properties"]
        assert "format" in props
        assert props["format"]["enum"] == [
            "cursor-mdc", "copilot-instructions",
        ]
        assert props["format"]["default"] == "cursor-mdc"

    def test_generate_cursor_rule_schema_includes_tool_use(self):
        gen = next(t for t in _TOOLS if t.name == "generate_cursor_rule")
        props = gen.inputSchema["properties"]
        assert "tool_use" in props
        assert props["tool_use"]["enum"] == ["available", "priority"]
        assert props["tool_use"]["default"] == "priority"


class TestToolHandlers:
    """The handlers operate on a real ProxyManager, but we can still exercise
    the pure-data paths (rule generation, mcp.json, full catalog snapshot)
    against a freshly constructed manager whose builtin hasn't been started."""

    @pytest.mark.asyncio
    async def test_generate_cursor_rule_handler_default_read_only(self):
        from zelosmcp.manager import ProxyManager

        m = ProxyManager(mandatory_config_path="")
        # builtin not started: catalog is empty -> rule reports no backends.
        result = await _HANDLERS["generate_cursor_rule"](m.builtin, {})
        assert len(result) == 1
        assert result[0].type == "text"
        body = result[0].text
        assert "alwaysApply: true" in body
        # Default access is read-only — both the directive and the
        # frontmatter description should reflect that.
        assert "(read-only mode)" in body

    @pytest.mark.asyncio
    async def test_generate_cursor_rule_access_read_write(self):
        from zelosmcp.manager import ProxyManager

        m = ProxyManager(mandatory_config_path="")
        result = await _HANDLERS["generate_cursor_rule"](
            m.builtin, {"access": "read-write"}
        )
        body = result[0].text
        assert "(read-write mode)" in body

    @pytest.mark.asyncio
    async def test_generate_cursor_rule_rejects_bad_access(self):
        from mcp.shared.exceptions import McpError

        from zelosmcp.manager import ProxyManager

        m = ProxyManager(mandatory_config_path="")
        with pytest.raises(McpError, match="Unknown access"):
            await _HANDLERS["generate_cursor_rule"](
                m.builtin, {"access": "bogus"}
            )

    @pytest.mark.asyncio
    async def test_generate_cursor_rule_format_copilot_instructions(self):
        """Handler accepts `format=copilot-instructions` and returns a
        body without the YAML frontmatter."""
        from zelosmcp.manager import ProxyManager

        m = ProxyManager(mandatory_config_path="")
        result = await _HANDLERS["generate_cursor_rule"](
            m.builtin, {"format": "copilot-instructions"}
        )
        body = result[0].text
        assert not body.startswith("---")
        assert "alwaysApply" not in body
        # Directive + body content still present.
        assert "Access mode: READ-ONLY" in body

    @pytest.mark.asyncio
    async def test_generate_cursor_rule_rejects_bad_format(self):
        from mcp.shared.exceptions import McpError

        from zelosmcp.manager import ProxyManager

        m = ProxyManager(mandatory_config_path="")
        with pytest.raises(McpError, match="Unknown format"):
            await _HANDLERS["generate_cursor_rule"](
                m.builtin, {"format": "bogus"}
            )

    @pytest.mark.asyncio
    async def test_generate_cursor_rule_rejects_bad_style(self):
        from mcp.shared.exceptions import McpError

        from zelosmcp.manager import ProxyManager

        m = ProxyManager(mandatory_config_path="")
        with pytest.raises(McpError, match="Unknown style"):
            await _HANDLERS["generate_cursor_rule"](
                m.builtin, {"style": "bogus"}
            )

    @pytest.mark.asyncio
    async def test_generate_cursor_rule_default_tool_use_is_priority(self):
        """Default `tool_use=priority` produces a body containing the
        priority directive even when no backends are loaded."""
        from zelosmcp.manager import ProxyManager

        m = ProxyManager(mandatory_config_path="")
        result = await _HANDLERS["generate_cursor_rule"](m.builtin, {})
        body = result[0].text
        # Even with empty backends, the priority directive only fires
        # when at least one user backend is loaded — but the default is
        # still `priority` so the explicit-`available` body must differ.
        explicit_available = await _HANDLERS["generate_cursor_rule"](
            m.builtin, {"tool_use": "available"}
        )
        assert body == explicit_available[0].text  # both empty bodies match
        # Explicit `priority` matches the default (round-trip).
        explicit_priority = await _HANDLERS["generate_cursor_rule"](
            m.builtin, {"tool_use": "priority"}
        )
        assert body == explicit_priority[0].text

    @pytest.mark.asyncio
    async def test_generate_cursor_rule_tool_use_available_omits_directive(self):
        """`tool_use=available` should drop the priority directive even
        when backends are present. We exercise the renderer directly
        because the handler's catalog is empty without running
        backends; the handler-level wiring is covered by the rejection
        test below and the integration round-trip test."""
        catalog = {
            "filesystem": _backend(
                [_tool("read_text_file", annotations={"readOnlyHint": True})]
            ),
        }
        priority = render_comprehensive_rule(catalog, tool_use="priority")
        available = render_comprehensive_rule(catalog, tool_use="available")
        assert "## Tool-use priority" in priority
        assert "## Tool-use priority" not in available

    @pytest.mark.asyncio
    async def test_generate_cursor_rule_rejects_bad_tool_use(self):
        from mcp.shared.exceptions import McpError

        from zelosmcp.manager import ProxyManager

        m = ProxyManager(mandatory_config_path="")
        with pytest.raises(McpError, match="Unknown tool_use"):
            await _HANDLERS["generate_cursor_rule"](
                m.builtin, {"tool_use": "bogus"}
            )

    @pytest.mark.asyncio
    async def test_get_aggregated_tool_catalog_returns_full_payload(self):
        """`zelosmcp__get_aggregated_tool_catalog` must return rich
        Tool/Prompt/Resource payloads (with inputSchema for tools), not
        just names. The HTTP `/api/catalog` shares the same helper, so
        this also locks in that endpoint's shape."""
        from zelosmcp.builtin import collect_backend_full_catalog
        from zelosmcp.manager import ProxyManager

        # Synthesize a manager whose only "running" backend is a stub
        # client_session that mimics filesystem's tool surface.
        from unittest.mock import AsyncMock

        m = ProxyManager(mandatory_config_path="")

        class _Stub:
            name = "filesystem"
            running = True
            backend_info = {"transport": "stdio"}
            client_session = AsyncMock()

        stub = _Stub()
        from mcp.types import Tool

        stub.client_session.list_tools = AsyncMock(
            return_value=type("R", (), {"tools": [
                Tool(name="read_text_file", description="Read a file", inputSchema={"type": "object"}),
            ]})()
        )
        stub.client_session.list_prompts = AsyncMock(
            return_value=type("R", (), {"prompts": []})()
        )
        stub.client_session.list_resources = AsyncMock(
            return_value=type("R", (), {"resources": []})()
        )
        stub.client_session.list_resource_templates = AsyncMock(
            return_value=type("R", (), {"resourceTemplates": []})()
        )
        m.servers["filesystem"] = stub

        catalog = await collect_backend_full_catalog(m, skip_self=True)
        assert "filesystem" in catalog
        fs = catalog["filesystem"]
        assert fs["transport"] == "stdio"
        assert fs["running"] is True
        assert isinstance(fs["tools"], list) and len(fs["tools"]) == 1
        # Tool entry shape — what the UI renders.
        tool = fs["tools"][0]
        assert tool["name"] == "read_text_file"
        assert tool["description"] == "Read a file"
        assert "inputSchema" in tool
        # Empty capabilities are [] (no error).
        assert fs["prompts"] == []
        assert fs["resources"] == []
        assert fs["resourceTemplates"] == []

    @pytest.mark.asyncio
    async def test_full_catalog_silent_skips_method_not_found(self):
        """A backend that doesn't implement prompts/resources (returns
        -32601) is coerced to an empty list, mirroring the aggregator's
        existing behavior."""
        from unittest.mock import AsyncMock

        from mcp.shared.exceptions import McpError
        from mcp.types import ErrorData, METHOD_NOT_FOUND, Tool

        from zelosmcp.builtin import collect_backend_full_catalog
        from zelosmcp.manager import ProxyManager

        m = ProxyManager(mandatory_config_path="")

        class _Stub:
            name = "filesystem"
            running = True
            backend_info = {"transport": "stdio"}
            client_session = AsyncMock()

        stub = _Stub()
        stub.client_session.list_tools = AsyncMock(
            return_value=type("R", (), {"tools": [
                Tool(name="read_text_file", description="x", inputSchema={"type": "object"}),
            ]})()
        )
        nf = McpError(ErrorData(code=METHOD_NOT_FOUND, message="Method not found"))
        stub.client_session.list_prompts = AsyncMock(side_effect=nf)
        stub.client_session.list_resources = AsyncMock(side_effect=nf)
        stub.client_session.list_resource_templates = AsyncMock(side_effect=nf)
        m.servers["filesystem"] = stub

        catalog = await collect_backend_full_catalog(m, skip_self=True)
        assert catalog["filesystem"]["prompts"] == []
        assert catalog["filesystem"]["resources"] == []
        assert catalog["filesystem"]["resourceTemplates"] == []

    @pytest.mark.asyncio
    async def test_generate_cursor_mcp_json_aggregate(self):
        from zelosmcp.manager import ProxyManager

        m = ProxyManager(mandatory_config_path="")
        result = await _HANDLERS["generate_cursor_mcp_json"](
            m.builtin, {"shape": "aggregate", "host": "127.0.0.1:9000"}
        )
        snippet = json.loads(result[0].text)
        assert "zelosmcp-aggregate" in snippet["mcpServers"]
        assert snippet["mcpServers"]["zelosmcp-aggregate"]["url"] == (
            "http://127.0.0.1:9000/mcp"
        )

    @pytest.mark.asyncio
    async def test_start_server_refuses_self_targeting(self):
        from mcp.shared.exceptions import McpError

        from zelosmcp.manager import ProxyManager

        m = ProxyManager(mandatory_config_path="")
        with pytest.raises(McpError, match="builtin"):
            await _HANDLERS["start_server"](m.builtin, {"name": NAME})
        with pytest.raises(McpError, match="builtin"):
            await _HANDLERS["stop_server"](m.builtin, {"name": NAME})

    @pytest.mark.asyncio
    async def test_start_server_requires_name(self):
        from mcp.shared.exceptions import McpError

        from zelosmcp.manager import ProxyManager

        m = ProxyManager(mandatory_config_path="")
        with pytest.raises(McpError, match="`name` is required"):
            await _HANDLERS["start_server"](m.builtin, {})
