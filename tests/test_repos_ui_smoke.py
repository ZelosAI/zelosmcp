"""Smoke test: assert the Repositories UI markup is wired up.

The UI is one big HTML template string in ``zelosmcp.ui.HTML_TEMPLATE``.
A regression in any of the markup blocks below would silently break the
right-column panel or the middle-pane editor, so we lock in the structural
landmarks. We only assert on stable IDs and class names — never on
copy or styling — so cosmetic tweaks don't trip these tests.
"""
from __future__ import annotations

import pytest

from zelosmcp.ui import HTML_TEMPLATE


# ── Right-column collapsible panel ─────────────────────────────────────


class TestRightColumnPanel:
    def test_repos_section_present(self):
        assert 'id="repos-section"' in HTML_TEMPLATE

    def test_collapsible_default_state_is_collapsed(self):
        # The panel must start collapsed (aria-expanded="false") so the
        # initial scan only fires when the user opens it.
        assert 'id="repos-toggle"' in HTML_TEMPLATE
        assert 'aria-expanded="false"' in HTML_TEMPLATE

    def test_collapse_target_is_hidden_by_default(self):
        # Use the `hidden` HTML attribute for the initial state so the
        # panel is invisible without JS.
        assert 'id="repos-collapse"' in HTML_TEMPLATE
        # The hidden attribute should appear inline on the collapse div
        # at template emit time — JS toggles it later.
        assert 'id="repos-collapse" hidden' in HTML_TEMPLATE

    def test_filter_input_present(self):
        assert 'id="repos-filter"' in HTML_TEMPLATE
        assert 'oninput="onReposFilter()"' in HTML_TEMPLATE

    def test_repos_list_container_present(self):
        assert 'id="repos-list"' in HTML_TEMPLATE

    def test_count_badge_present(self):
        assert 'id="repos-count"' in HTML_TEMPLATE

    def test_refresh_button_present(self):
        assert 'id="repos-refresh-btn"' in HTML_TEMPLATE
        assert 'onclick="refreshRepos()"' in HTML_TEMPLATE

    def test_toggle_handler_wired(self):
        assert 'onclick="toggleReposPanel()"' in HTML_TEMPLATE


# ── Middle-pane repo-details view ──────────────────────────────────────


class TestRepoDetailsView:
    def test_view_section_present(self):
        # Mirrors the existing data-view="server-details" pattern.
        assert 'data-view="repo-details"' in HTML_TEMPLATE

    def test_path_metadata_block_present(self):
        assert 'id="repo-details-paths"' in HTML_TEMPLATE
        assert 'id="repo-details-meta"' in HTML_TEMPLATE
        assert 'id="repo-details-title"' in HTML_TEMPLATE

    @pytest.mark.parametrize(
        "select_id",
        [
            # Rule-format selection moved from a <select id="repo-rule-format">
            # to the IDE tab bar (data-fmt buttons, see
            # test_format_options_match_api). The remaining per-rule controls
            # are still <select>s.
            "repo-rule-tool-use",
            "repo-rule-access",
            "repo-rule-style",
        ],
    )
    def test_form_select_present(self, select_id):
        assert f'id="{select_id}"' in HTML_TEMPLATE

    def test_globs_input_present(self):
        assert 'id="repo-rule-globs"' in HTML_TEMPLATE

    def test_format_options_match_api(self):
        # Format selection is the IDE tab bar; the two format keys must match
        # RULE_RELATIVE_PATHS in zelosmcp.repos so the UI can't request
        # something the backend rejects. The tabs carry the key in data-fmt.
        assert 'data-fmt="cursor-mdc"' in HTML_TEMPLATE
        assert 'data-fmt="copilot-instructions"' in HTML_TEMPLATE

    def test_action_buttons_wired(self):
        # These actions are no longer wired via inline onclick= attributes;
        # they're invoked from the JS (e.g. onRepoIdeTabClick → previewRepoRule,
        # the save/index buttons' handlers). Assert the call sites exist so a
        # rename still trips this smoke test.
        assert "previewRepoRule()" in HTML_TEMPLATE
        assert "saveRepoRule()" in HTML_TEMPLATE
        assert "indexRepo()" in HTML_TEMPLATE

    def test_preview_pane_present(self):
        assert 'id="repo-rule-preview"' in HTML_TEMPLATE

    def test_status_line_present(self):
        assert 'id="repo-rule-status"' in HTML_TEMPLATE


# ── JS plumbing landmarks ──────────────────────────────────────────────


class TestJsLandmarks:
    """The JS lives in the same template string. Spot-check that the
    public functions wired to onclick handlers above actually exist —
    a missing function = silent NoOp at runtime, which is exactly the
    kind of regression a smoke test should catch."""

    @pytest.mark.parametrize(
        "fn_name",
        [
            "function loadRepos",
            "function renderReposList",
            "function showRepoDetails",
            "function renderRepoDetails",
            "function toggleReposPanel",
            "function refreshRepos",
            "function previewRepoRule",
            "function saveRepoRule",
            "function indexRepo",
            "function onReposFilter",
        ],
    )
    def test_function_defined(self, fn_name):
        assert fn_name in HTML_TEMPLATE

    def test_localstorage_keys_used(self):
        assert "zelosmcp:repos:expanded" in HTML_TEMPLATE
        assert "zelosmcp:repos:filter" in HTML_TEMPLATE

    def test_set_view_invocation_for_repo_details(self):
        # The middle-pane swap goes through the existing setView() helper.
        assert 'setView("repo-details")' in HTML_TEMPLATE

    def test_calls_existing_endpoints(self):
        # Wired to the routes we registered in app.py.
        assert "/api/repos" in HTML_TEMPLATE
        assert "/api/repos/write-rule" in HTML_TEMPLATE
        # indexRepo() drives the pincher index_project asset extension rather
        # than a dedicated /api/repos/index route (which was never registered).
        assert "/api/assets/extension/pincher/index_project/invoke" in HTML_TEMPLATE
        # Preview reuses the existing rule endpoint so the preview is
        # byte-identical to what saveRepoRule() will POST.
        assert "/api/cursor-rule?" in HTML_TEMPLATE
