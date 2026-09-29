# SPDX-License-Identifier: MIT
"""Flake refs as NRI mount sources, and the build policy that gates them.

`nixkube/redis: /knix=github:NixOS/nixpkgs/<rev>#redis` used to be parsed and
then dropped: injection only started for literal store paths, so the container
ran without its mount. Now the build task resolves the ref to a store path
first, and the node's build policy decides whether it may.
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
    def test_a_flake_source_is_kept_verbatim(self) -> None:
        """`Path` would fold the `//` of a URL-style ref."""
        ref = "git+https://example.com/app.git?ref=main#app"
        result = parse_store_mounts({"nixkube/app": f"/app={ref}"}, "app", SYSTEM)
        assert result == {Path("/app"): ref}

    def test_a_store_source_is_still_a_path(self) -> None:
        result = parse_store_mounts(
            {"nixkube/app": "/app=/nix/store/abc-app"}, "app", SYSTEM
        )
        assert result == {Path("/app"): Path("/nix/store/abc-app")}

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

    async def mount_in_container(pid, bundle, _tree, mounts, nix_rw, farm_paths):
        calls["mounts"] = mounts

    monkeypatch.setattr(nri_server, "NRI_CONTAINERS", tmp_path / "containers")
    monkeypatch.setattr(nri_server, "get_build_args", build_args)
    monkeypatch.setattr(nri_server, "build_flake_ref", build_flake_ref)
    monkeypatch.setattr(nri_server, "fetch_packages", fetch_packages)
    monkeypatch.setattr(nri_server, "prepare_volume", prepare_volume)
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


@pytest.mark.asyncio
async def test_an_expression_source_is_built_as_an_expression(
    monkeypatch, tmp_path: Path
) -> None:
    out = tmp_path / "abc-result"
    out.mkdir()
    built: list[str] = []

    async def build_nix_expr(expr, _gc_root, _extra_args):
        built.append(expr)
        return out

    async def not_a_flake(*_args):
        pytest.fail("an expression was built as a flake ref")

    async def nothing(*_args, **_kwargs):
        return []

    async def prepare_volume(_volume, paths, _unused, bind_farm):
        return SimpleNamespace(bind_farm=True, paths=set(paths))

    monkeypatch.setattr(nri_server, "NRI_CONTAINERS", tmp_path / "containers")
    monkeypatch.setattr(nri_server, "get_build_args", nothing)
    monkeypatch.setattr(nri_server, "build_nix_expr", build_nix_expr)
    monkeypatch.setattr(nri_server, "build_flake_ref", not_a_flake)
    monkeypatch.setattr(nri_server, "fetch_packages", nothing)
    monkeypatch.setattr(nri_server, "prepare_volume", prepare_volume)
    monkeypatch.setattr(nri_server, "mount_in_container", nothing)

    plugin = nri_server.NriPlugin.__new__(nri_server.NriPlugin)
    plugin.zmq_server = _Zmq()
    expr = 'builtins.toFile "test" "value"'
    await plugin._build_and_mount(
        SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None),
        "c0",
        set(),
        {Path("/etc/result"): expr},
        False,
    )

    assert built == [expr]


@pytest.mark.asyncio
async def test_a_container_with_only_a_flake_mount_is_not_skipped(monkeypatch) -> None:
    """The early "nothing to build" exit used to look at store paths alone."""
    built: list[object] = []

    async def build_and_mount(*args):
        built.append(args)
        return set()

    async def no_event(*_args, **_kwargs) -> None:
        pass

    class Zmq:
        def __init__(self) -> None:
            from src.nri.builds import BuildRegistry

            self.pending_builds = BuildRegistry()
            self.build_status: dict[str, dict[str, str]] = {}

        async def publish_build_progress(self, _c: str) -> None:
            pass

        async def publish_build_complete(self, _c: str) -> None:
            pass

    monkeypatch.setattr(nri_server, "report_event", no_event)
    monkeypatch.setattr(nri_server, "schedule_copy_to_cache", lambda *_a: None)

    plugin = nri_server.NriPlugin.__new__(nri_server.NriPlugin)
    plugin.zmq_server = Zmq()
    plugin.tasks = None
    plugin._build_and_mount = build_and_mount

    await plugin._spawn_build_task("c0", "redis", None, set(), {Path("/knix"): FLAKE})

    assert built, "a container whose only source is a flake ref was never built"


def test_a_failure_reason_names_the_error_inside_the_group() -> None:
    from src.errors import BuildError

    inner = BuildError(
        "Failed to build Nix expression",
        logs="evaluating...\nerror: path '/nix/store/x-probe' is required, "
        "but there is no substituter that can build it\n",
    )
    reason = nri_server._failure_reason(ExceptionGroup("unhandled", [inner]))
    assert reason.startswith("Failed to build Nix expression: ")
    assert reason.endswith("no substituter that can build it")
