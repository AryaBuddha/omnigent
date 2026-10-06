"""E2E: a non-git workspace's Changes panel must surface that tracking is limited.

Outside a git repo only writes routed through ``record_change()`` (the agent's
file tools, observed native-harness edits and the REST filesystem endpoints) are
tracked, so a file written straight to disk by a shell command or an external
editor never reaches ``GET .../changes``. The Changes tab used to render the bare
"No workspace changes yet" state for it, indistinguishable from a clean
workspace. This drives that journey end to end in the real SPA, server and
runner and expects the limited-tracking notice instead.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e_ui.conftest import (
    _build_hello_world_bundle,
    _ensure_runner_online,
    _server_state,
    open_right_rail,
)

_EXTERNAL_FILE = "external_note.txt"
_EXTERNAL_CONTENT = "written straight to disk, bypassing record_change\n"


@pytest.fixture
def non_git_external_write_session(
    live_server: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A runner-bound session in a non-git workspace with an external on-disk write.

    The plain directory is pinned via ``metadata.workspace``, which the runner's
    per-session registry resolves against; the file is written before the
    session opens, bypassing ``record_change()`` like an external editor does.

    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path: Per-test dir for the non-git workspace (outside any repo).
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id, workspace)``.
    """
    workspace = tmp_path / "plain-folder"
    workspace.mkdir()
    (workspace / _EXTERNAL_FILE).write_text(_EXTERNAL_CONTENT)

    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    bundle = _build_hello_world_bundle()
    create = post_session_bundle(
        httpx.post,
        f"{live_server}/v1/sessions",
        bundle,
        metadata={"workspace": str(workspace)},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]
    bind_session_runner(httpx.patch, live_server, session_id, runner_id, timeout=10.0)
    try:
        yield (live_server, session_id, workspace)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


def test_non_git_external_write_surfaces_limited_tracking(
    page: Page,
    non_git_external_write_session: tuple[str, str, Path],
) -> None:
    """A non-git workspace with an untracked on-disk change surfaces the limitation."""
    base_url, session_id, workspace = non_git_external_write_session
    target = workspace / _EXTERNAL_FILE

    assert target.exists(), "fixture did not write the external file to disk"

    # The externally written file is invisible to the non-git registry: full
    # tracking is out of scope, so the changes list stays empty across the fix.
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/environments/default/changes",
        timeout=30.0,
    )
    resp.raise_for_status()
    paths = [entry["path"] for entry in (resp.json().get("data") or [])]
    assert _EXTERNAL_FILE not in paths, (
        f"non-git registry unexpectedly tracked an external write: {paths}"
    )

    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    changes_tab = rail.get_by_role("tab", name=re.compile("^Changes"))
    changes_tab.click()
    expect(changes_tab).to_have_attribute("aria-selected", "true")

    # Correct behavior: with a real on-disk change it cannot track, the panel
    # says why tracking is limited instead of rendering the bare empty state.
    expect(rail.get_by_text("Limited change tracking")).to_be_visible(timeout=30_000)
    expect(rail.get_by_text(re.compile("isn't a Git repository"))).to_be_visible()

    # The silent empty state — the buggy rendering — must not appear, and the
    # degraded state is an explanation, not a load failure.
    expect(rail.get_by_text("No workspace changes yet")).to_have_count(0)
    expect(rail.get_by_text(re.compile(r"^Failed to load:"))).to_have_count(0)
