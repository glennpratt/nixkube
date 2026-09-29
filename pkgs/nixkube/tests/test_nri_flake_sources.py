# SPDX-License-Identifier: MIT
"""Flake refs and expressions as NRI mount sources.

`nixkube/redis: /knix=github:NixOS/nixpkgs/<rev>#redis` used to be parsed and
then dropped: injection only started for literal store paths, so the container
ran without its mount. Now the build task resolves the source to a store path
first, and from there it is mounted like any other.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.nri import server as nri_server
from src.nri.annotations import parse_store_mounts
from src.nri.builds import BuildRegistry

SYSTEM = "x86_64-linux"
FLAKE = "github:NixOS/nixpkgs/7a0f122f5090cf4c2ade2a13a0e229d4e19ba71f#redis"
LOG = SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None)


def test_a_url_style_flake_ref_survives_parsing() -> None:
    """`Path` would fold the `//` of a URL-style ref."""
    ref = "git+https://example.com/app.git?ref=main#app"
    result = parse_store_mounts({"nixkube/app": f"/app={ref}"}, "app", SYSTEM)
    assert result == {Path("/app"): ref}


class _Zmq:
    async def wait_for_pid(self, _container_id: str):
        return (4242, "/run/bundle")


def _stub_mounting(monkeypatch, tmp_path: Path, calls: dict[str, object]) -> None:
    async def build_args() -> list[str]:
        return ["--builders", "ssh-ng://builder"]

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
    monkeypatch.setattr(nri_server, "fetch_packages", fetch_packages)
    monkeypatch.setattr(nri_server, "prepare_volume", prepare_volume)
    monkeypatch.setattr(nri_server, "reachable_rootfs", reachable_rootfs)
    monkeypatch.setattr(nri_server, "mount_in_container", mount_in_container)


def _plugin() -> nri_server.NriPlugin:
    plugin = nri_server.NriPlugin.__new__(nri_server.NriPlugin)
    plugin.zmq_server = _Zmq()
    return plugin


@pytest.mark.asyncio
async def test_a_flake_source_is_built_mounted_and_returned(
    monkeypatch, tmp_path: Path
) -> None:
    redis = tmp_path / "store" / "abc-redis"
    redis.mkdir(parents=True)
    calls: dict[str, object] = {}

    async def build_flake_ref(ref, gc_root, extra_args):
        calls["flake"] = (ref, gc_root, extra_args)
        return redis

    _stub_mounting(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(nri_server, "build_flake_ref", build_flake_ref)

    realised = await _plugin()._build_and_mount(
        LOG, "c0", set(), {Path("/knix"): FLAKE}, False
    )

    ref, gc_root, extra_args = calls["flake"]
    assert ref == FLAKE
    assert gc_root.is_relative_to(tmp_path / "containers" / "c0"), (
        "the gc root must live and die with the container's volume"
    )
    assert extra_args == ["--builders", "ssh-ng://builder"]
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

    _stub_mounting(monkeypatch, tmp_path, {})
    monkeypatch.setattr(nri_server, "build_nix_expr", build_nix_expr)
    monkeypatch.setattr(nri_server, "build_flake_ref", not_a_flake)

    expr = 'builtins.toFile "test" "value"'
    await _plugin()._build_and_mount(
        LOG, "c0", set(), {Path("/etc/result"): expr}, False
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
            self.pending_builds = BuildRegistry()
            self.build_status: dict[str, dict[str, str]] = {}

        async def publish_build_progress(self, _c: str) -> None:
            pass

        async def publish_build_complete(self, _c: str) -> None:
            pass

        async def publish_build_failed(self, _c: str, _reason: str) -> None:
            pass

    monkeypatch.setattr(nri_server, "report_event", no_event)
    monkeypatch.setattr(nri_server, "schedule_copy_to_cache", lambda *_a: None)

    plugin = nri_server.NriPlugin.__new__(nri_server.NriPlugin)
    plugin.zmq_server = Zmq()
    plugin.tasks = None
    plugin._build_and_mount = build_and_mount

    await plugin._spawn_build_task("c0", "redis", None, set(), {Path("/knix"): FLAKE})

    assert built, "a container whose only source is a flake ref was never built"
