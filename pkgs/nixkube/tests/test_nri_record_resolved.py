# SPDX-License-Identifier: MIT
"""Recording what a pod's flake refs resolved to: an opt-in add-on.

The counterpart of a container status's image digest. It writes to the API,
so it only happens with `NRI_RECORD_RESOLVED`, and only for flake-ref
sources: a store path already says what it is.
"""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from src.nri import server as nri_server
from src.nri.builds import BuildRegistry

RESOLVED = {
    "/knix": {
        "source": "github:NixOS/nixpkgs/nixos-unstable#redis",
        "path": "/nix/store/kjirjpbcf52z7ygl7rvn0vf3vk4rgw3m-redis-8.10.2",
    }
}


class _Pod:
    def __init__(self, fail: bool = False) -> None:
        self.patches: list[dict] = []
        self.fail = fail

    async def patch(self, body: dict) -> None:
        if self.fail:
            raise RuntimeError("forbidden: cannot patch pods")
        self.patches.append(body)


@pytest.mark.asyncio
async def test_the_pod_is_annotated_per_container() -> None:
    pod = _Pod()
    await nri_server.record_resolved(pod, "app", RESOLVED)
    (body,) = pod.patches
    value = body["metadata"]["annotations"]["resolved.nixkube/app"]
    assert json.loads(value) == RESOLVED


@pytest.mark.asyncio
async def test_a_failed_patch_is_not_raised() -> None:
    """The container is running by now; this is only a record of it."""
    await nri_server.record_resolved(_Pod(fail=True), "app", RESOLVED)


class _Zmq:
    def __init__(self) -> None:
        self.pending_builds = BuildRegistry()
        self.build_status: dict[str, dict[str, str]] = {}

    async def publish_build_progress(self, _c: str) -> None:
        pass

    async def publish_build_complete(self, _c: str) -> None:
        pass

    async def publish_build_failed(self, _c: str, _reason: str) -> None:
        pass


async def _build(monkeypatch, *, enabled: bool, resolved: dict) -> _Pod:
    async def build_and_mount(*args):
        args[-1].update(resolved)  # the resolved_sources the task passed in
        return set()

    async def no_event(*_args, **_kwargs) -> None:
        pass

    monkeypatch.setattr(nri_server, "NRI_RECORD_RESOLVED", enabled)
    monkeypatch.setattr(nri_server, "report_event", no_event)
    monkeypatch.setattr(nri_server, "schedule_copy_to_cache", lambda *_a: None)

    pod = _Pod()
    plugin = nri_server.NriPlugin.__new__(nri_server.NriPlugin)
    plugin.zmq_server = _Zmq()
    plugin._build_and_mount = build_and_mount
    async with anyio.create_task_group() as tasks:
        plugin.tasks = tasks
        await plugin._spawn_build_task(
            "c0", "app", pod, set(), {Path("/knix"): RESOLVED["/knix"]["source"]}
        )
    return pod


@pytest.mark.asyncio
async def test_recorded_when_enabled(monkeypatch) -> None:
    pod = await _build(monkeypatch, enabled=True, resolved=RESOLVED)
    assert len(pod.patches) == 1


@pytest.mark.asyncio
async def test_nothing_is_written_when_disabled(monkeypatch) -> None:
    pod = await _build(monkeypatch, enabled=False, resolved=RESOLVED)
    assert pod.patches == [], "the default must not write to the API"


@pytest.mark.asyncio
async def test_nothing_is_written_without_flake_sources(monkeypatch) -> None:
    pod = await _build(monkeypatch, enabled=True, resolved={})
    assert pod.patches == []
