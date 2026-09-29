# SPDX-License-Identifier: MIT
"""The build policy: how much a node may do to realise a container's store.

`build` evaluates and builds, `substitute` evaluates and only fetches,
`pinned` takes store paths alone. The node sets the ceiling; a pod may only
tighten it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from nri.nri_pb2 import CreateContainerRequest

from src.nri import server as nri_server
from src.nri.annotations import parse_build_policy, parse_store_mounts
from src.nri.policy import (
    BuildPolicy,
    BuildPolicyError,
    effective_build_policy,
    nix_policy_args,
)

SYSTEM = "x86_64-linux"
FLAKE = "github:NixOS/nixpkgs/7a0f122f5090cf4c2ade2a13a0e229d4e19ba71f#redis"
# A real-looking store path: injection only notices the 32-character hash form.
REDIS = "/nix/store/kjirjpbcf52z7ygl7rvn0vf3vk4rgw3m-redis-8.10.2"


class TestPolicy:
    def test_parse_is_case_insensitive(self) -> None:
        assert BuildPolicy.parse("Substitute") is BuildPolicy.SUBSTITUTE

    def test_parse_rejects_unknown_values(self) -> None:
        with pytest.raises(BuildPolicyError, match="expected one of"):
            BuildPolicy.parse("sometimes")

    @pytest.mark.parametrize("requested", list(BuildPolicy))
    def test_a_pod_never_loosens_the_node(self, requested: BuildPolicy) -> None:
        assert (
            effective_build_policy(BuildPolicy.PINNED, requested) is BuildPolicy.PINNED
        )

    def test_a_pod_may_tighten_the_node(self) -> None:
        assert (
            effective_build_policy(BuildPolicy.BUILD, BuildPolicy.SUBSTITUTE)
            is BuildPolicy.SUBSTITUTE
        )

    def test_no_request_means_the_node_policy(self) -> None:
        assert (
            effective_build_policy(BuildPolicy.SUBSTITUTE, None)
            is BuildPolicy.SUBSTITUTE
        )

    def test_only_build_may_build(self) -> None:
        assert nix_policy_args(BuildPolicy.BUILD) == []
        for policy in (BuildPolicy.SUBSTITUTE, BuildPolicy.PINNED):
            assert nix_policy_args(policy) == ["--max-jobs", "0", "--builders", ""]


class TestAnnotations:
    def test_no_annotation_means_no_request(self) -> None:
        assert parse_build_policy({}, "app", SYSTEM) is None

    def test_the_container_annotation_wins(self) -> None:
        annotations = {"nixkube/pod-build": "pinned", "nixkube/app-build": "substitute"}
        assert parse_build_policy(annotations, "app", SYSTEM) is BuildPolicy.SUBSTITUTE
        assert parse_build_policy(annotations, "other", SYSTEM) is BuildPolicy.PINNED

    def test_the_strictest_of_several_counts(self) -> None:
        annotations = {
            "nixkube/app-build": "build",
            f"nixkube/app-build@{SYSTEM}": "pinned",
        }
        assert parse_build_policy(annotations, "app", SYSTEM) is BuildPolicy.PINNED

    def test_a_policy_annotation_is_not_a_mount(self) -> None:
        """`nixkube/app-build` also matches the mount pattern for `app`."""
        annotations = {"nixkube/app-build": "pinned"}
        assert parse_store_mounts(annotations, "app", SYSTEM) == {}

    def test_a_misspelt_policy_fails_closed(self) -> None:
        with pytest.raises(BuildPolicyError):
            parse_build_policy({"nixkube/pod-build": "pined"}, "app", SYSTEM)


class _Stream:
    def __init__(self, request: CreateContainerRequest) -> None:
        self._request = request
        self.sent: list[object] = []

    async def recv_message(self) -> CreateContainerRequest:
        return self._request

    async def send_message(self, message: object) -> None:
        self.sent.append(message)


def _request(annotations: dict[str, str]) -> CreateContainerRequest:
    request = CreateContainerRequest()
    request.pod.namespace = "default"
    request.pod.name = "redis"
    request.pod.annotations.update(annotations)
    request.container.name = "redis"
    request.container.id = "c0"
    return request


class _RefusalZmq:
    def __init__(self) -> None:
        from src.nri.builds import BuildRegistry

        self.pending_builds = BuildRegistry()
        self.build_status: dict[str, dict[str, str]] = {}


@pytest.mark.asyncio
class TestRefusal:
    """A refused container fails in its wait hook, with the reason.

    Never by raising: containerd closes a plugin that fails a request, and
    every container created before it reconnects starts with no injection.
    """

    async def _create(
        self, monkeypatch, node: BuildPolicy, annotations: dict[str, str]
    ):
        events: list[dict[str, object]] = []

        async def record_event(*_args, **kwargs) -> None:
            events.append(kwargs)

        monkeypatch.setattr(nri_server, "get_current_system", lambda: SYSTEM)
        monkeypatch.setattr(nri_server, "node_build_policy", lambda: node)
        monkeypatch.setattr(nri_server, "report_event", record_event)
        monkeypatch.setattr(nri_server.shutil, "which", lambda name: f"/bin/{name}")

        plugin = nri_server.NriPlugin.__new__(nri_server.NriPlugin)
        plugin.zmq_server = _RefusalZmq()
        plugin.nri_wait_bin = "/bin/wait"
        stream = _Stream(_request(annotations))
        async with anyio.create_task_group() as tasks:
            plugin.tasks = tasks
            # Stand in for the build, so the negative control can see one start.
            spawned: list[str] = []

            async def spawn(container_id: str, *_args) -> None:
                spawned.append(container_id)

            plugin._spawn_build_task = spawn
            with anyio.fail_after(1.5):
                # Under containerd's ~2s: a slower handler loses the plugin.
                await nri_server.NriPlugin.CreateContainer(plugin, stream)
        plugin.spawned = spawned
        return plugin, stream, events

    def _assert_refused(self, plugin, stream, events, match: str) -> None:
        assert len(stream.sent) == 1, "the create must be answered, not raised"
        assert stream.sent[0].adjust.hooks.create_runtime, "no wait hook to fail in"
        status = plugin.zmq_server.build_status["c0"]
        assert status["status"] == "failed"
        assert match in status["reason"]
        assert plugin.spawned == [], "a build was spawned"
        assert [e["reason"] for e in events] == ["BuildPolicyRefused"]

    async def test_a_pinned_node_refuses_a_flake(self, monkeypatch) -> None:
        plugin, stream, events = await self._create(
            monkeypatch, BuildPolicy.PINNED, {"nixkube/redis": f"/knix={FLAKE}"}
        )
        self._assert_refused(plugin, stream, events, "pinned")

    async def test_a_pod_can_pin_itself(self, monkeypatch) -> None:
        plugin, stream, events = await self._create(
            monkeypatch,
            BuildPolicy.BUILD,
            {"nixkube/redis": f"/knix={FLAKE}", "nixkube/pod-build": "pinned"},
        )
        self._assert_refused(plugin, stream, events, "pinned")

    async def test_a_misspelt_policy_is_refused_not_raised(self, monkeypatch) -> None:
        plugin, stream, events = await self._create(
            monkeypatch,
            BuildPolicy.BUILD,
            {
                "nixkube/redis": f"/knix={REDIS}",
                "nixkube/pod-build": "pined",
            },
        )
        self._assert_refused(plugin, stream, events, "unknown build policy")

    async def test_a_pinned_node_still_takes_store_paths(self, monkeypatch) -> None:
        """The negative control: pinned refuses evaluation, not injection.

        The store path is injected and its build spawned as usual.
        """
        plugin, stream, events = await self._create(
            monkeypatch,
            BuildPolicy.PINNED,
            {"nixkube/redis": f"/knix={REDIS}"},
        )
        assert len(stream.sent) == 1
        assert stream.sent[0].adjust.hooks.create_runtime, "it was never injected"
        assert "c0" not in plugin.zmq_server.build_status
        assert plugin.spawned == ["c0"]
        assert events == []


class _Zmq:
    async def wait_for_pid(self, _container_id: str):
        return (4242, "/run/bundle")


@pytest.mark.asyncio
async def test_a_flake_mount_is_resolved_built_under_policy_and_mounted(
    monkeypatch, tmp_path: Path
) -> None:
    store = tmp_path / "store"
    redis = store / "abc-redis"
    redis.mkdir(parents=True)
    calls: dict[str, object] = {}

    async def build_args() -> list[str]:
        return ["--builders", "ssh-ng://builder"]

    async def build_flake_ref(ref, gc_root, extra_args):
        calls["flake"] = (ref, gc_root, extra_args)
        return redis

    async def fetch_packages(paths, _gc_root, _extra_args):
        calls["fetched"] = set(paths)

    async def prepare_volume(_volume, paths, _unused, bind_farm):
        return SimpleNamespace(bind_farm=True, paths=set(paths))

    async def reachable_rootfs(_pid, _bundle):
        return "/proc/4242/root"

    async def mount_in_container(pid, rootfs, _tree, mounts, nix_rw, farm_paths):
        calls["mounts"] = mounts

    monkeypatch.setattr(nri_server, "NRI_CONTAINERS", tmp_path / "containers")
    monkeypatch.setattr(nri_server, "get_build_args", build_args)
    monkeypatch.setattr(nri_server, "build_flake_ref", build_flake_ref)
    monkeypatch.setattr(nri_server, "fetch_packages", fetch_packages)
    monkeypatch.setattr(nri_server, "prepare_volume", prepare_volume)
    monkeypatch.setattr(nri_server, "reachable_rootfs", reachable_rootfs)
    monkeypatch.setattr(nri_server, "mount_in_container", mount_in_container)

    plugin = nri_server.NriPlugin.__new__(nri_server.NriPlugin)
    plugin.zmq_server = _Zmq()

    realised = await plugin._build_and_mount(
        SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None),
        "c0",
        set(),
        {Path("/knix"): FLAKE},
        False,
        BuildPolicy.SUBSTITUTE,
    )

    ref, gc_root, extra_args = calls["flake"]
    assert ref == FLAKE
    assert gc_root.is_relative_to(tmp_path / "containers" / "c0")
    assert extra_args[-4:] == ["--max-jobs", "0", "--builders", ""], (
        "the policy's arguments must come last, to override the builders"
    )
    assert calls["fetched"] == {redis}
    assert calls["mounts"] == [(redis, Path("/knix"))]
    assert realised == {redis}
