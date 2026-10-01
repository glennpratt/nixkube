# SPDX-License-Identifier: MIT
"""Flake refs resolve the way OCI image refs pull.

A locked ref is a digest: one resolution per node, remembered. A floating ref
is a tag: re-resolved at every start (`always`, the default) unless the pod
asks for `if-not-present`.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.nri import resolution
from src.nri import server as nri_server
from src.nri.annotations import parse_pull_policy
from src.nri.resolution import (
    PullPolicy,
    PullPolicyError,
    Resolutions,
    is_locked_ref,
)

SYSTEM = "x86_64-linux"
REV = "7a0f122f5090cf4c2ade2a13a0e229d4e19ba71f"
LOCKED = f"github:NixOS/nixpkgs/{REV}#redis"
FLOATING = "github:NixOS/nixpkgs/nixos-unstable#redis"
LOG = SimpleNamespace(info=lambda *a, **k: None, debug=lambda *a, **k: None)


class TestLockedRefs:
    @pytest.mark.parametrize(
        "ref",
        [
            LOCKED,
            f"github:NixOS/nixpkgs?rev={REV}#redis",
            f"git+https://example.com/app.git?ref=main&rev={REV}#app",
            "https://example.com/src.tar.gz?narHash=sha256-AAAA#app",
            f"gitlab:group/project/{REV}",
        ],
    )
    def test_a_ref_naming_one_revision_is_locked(self, ref: str) -> None:
        assert is_locked_ref(ref)

    @pytest.mark.parametrize(
        "ref",
        [
            FLOATING,
            "github:NixOS/nixpkgs#redis",
            "nixpkgs#redis",
            "git+https://example.com/app.git?ref=main#app",
            # A short hash could be a branch name.
            "github:NixOS/nixpkgs/7a0f122#redis",
            'builtins.toFile "a" "b"',
        ],
    )
    def test_anything_else_is_floating(self, ref: str) -> None:
        assert not is_locked_ref(ref)


class TestRecord:
    def test_it_survives_a_restart(self, tmp_path: Path) -> None:
        path = tmp_path / "abc-redis"
        path.mkdir()
        Resolutions(tmp_path / "r.json").put(LOCKED, SYSTEM, path)
        assert Resolutions(tmp_path / "r.json").get(LOCKED, SYSTEM) == path

    def test_it_is_per_system(self, tmp_path: Path) -> None:
        path = tmp_path / "abc-redis"
        path.mkdir()
        record = Resolutions(tmp_path / "r.json")
        record.put(LOCKED, SYSTEM, path)
        assert record.get(LOCKED, "aarch64-linux") is None

    def test_a_collected_path_is_forgotten(self, tmp_path: Path) -> None:
        record = Resolutions(tmp_path / "r.json")
        record.put(LOCKED, SYSTEM, tmp_path / "gone")
        assert record.get(LOCKED, SYSTEM) is None

    def test_an_unreadable_file_is_a_fresh_start(self, tmp_path: Path) -> None:
        (tmp_path / "r.json").write_text("{not json")
        assert Resolutions(tmp_path / "r.json").get(LOCKED, SYSTEM) is None

    def test_a_failed_write_is_not_remembered(self, tmp_path: Path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("")
        record = Resolutions(blocker / "r.json")  # its parent is a file
        with pytest.raises(OSError):
            record.put(LOCKED, SYSTEM, tmp_path)
        assert record.get(LOCKED, SYSTEM) is None


class TestPullPolicyAnnotation:
    def test_always_by_default(self) -> None:
        assert parse_pull_policy({}, "app", SYSTEM) is PullPolicy.ALWAYS

    def test_the_container_wins(self) -> None:
        annotations = {
            "nixkube/pod-pull": "always",
            "nixkube/app-pull": "if-not-present",
        }
        assert (
            parse_pull_policy(annotations, "app", SYSTEM) is PullPolicy.IF_NOT_PRESENT
        )

    def test_an_unknown_value_raises(self) -> None:
        with pytest.raises(PullPolicyError):
            parse_pull_policy({"nixkube/pod-pull": "sometimes"}, "app", SYSTEM)

    def test_a_policy_annotation_is_not_a_mount(self) -> None:
        from src.nri.annotations import parse_store_mounts

        assert parse_store_mounts({"nixkube/app-pull": "always"}, "app", SYSTEM) == {}


class _Zmq:
    async def wait_for_pid(self, _container_id: str):
        return (4242, "/run/bundle")


async def _mount(
    monkeypatch, tmp_path: Path, source: str, pull: PullPolicy, builds: list
):
    """Run `_build_and_mount` for one source with everything below it stubbed."""
    out = tmp_path / "store" / f"abc-{len(builds)}"

    async def build_flake_ref(ref, _gc_root, extra_args):
        builds.append(extra_args)
        out.mkdir(parents=True, exist_ok=True)
        return out

    async def nothing(*_args, **_kwargs):
        return []

    async def prepare_volume(_volume, paths, _unused, bind_farm):
        return SimpleNamespace(bind_farm=True, paths=set(paths))

    async def reachable_rootfs(_pid, _bundle):
        return "/proc/4242/root"

    monkeypatch.setattr(nri_server, "NRI_CONTAINERS", tmp_path / "containers")
    monkeypatch.setattr(nri_server, "get_current_system", lambda: SYSTEM)
    monkeypatch.setattr(nri_server, "get_build_args", nothing)
    monkeypatch.setattr(nri_server, "build_flake_ref", build_flake_ref)
    monkeypatch.setattr(nri_server, "fetch_packages", nothing)
    monkeypatch.setattr(nri_server, "prepare_volume", prepare_volume)
    monkeypatch.setattr(nri_server, "reachable_rootfs", reachable_rootfs)
    monkeypatch.setattr(nri_server, "mount_in_container", nothing)

    plugin = nri_server.NriPlugin.__new__(nri_server.NriPlugin)
    plugin.zmq_server = _Zmq()
    return await plugin._build_and_mount(
        LOG, "c0", set(), {Path("/knix"): source}, False, pull_policy=pull
    )


@pytest.mark.asyncio
class TestResolution:
    async def test_a_locked_ref_resolves_once_per_node(
        self, monkeypatch, tmp_path
    ) -> None:
        builds: list = []
        first = await _mount(monkeypatch, tmp_path, LOCKED, PullPolicy.ALWAYS, builds)
        second = await _mount(monkeypatch, tmp_path, LOCKED, PullPolicy.ALWAYS, builds)
        assert len(builds) == 1, "a locked ref was evaluated again"
        assert first == second

    async def test_a_floating_ref_is_refetched_every_time(
        self, monkeypatch, tmp_path
    ) -> None:
        builds: list = []
        await _mount(monkeypatch, tmp_path, FLOATING, PullPolicy.ALWAYS, builds)
        await _mount(monkeypatch, tmp_path, FLOATING, PullPolicy.ALWAYS, builds)
        assert len(builds) == 2
        assert all("--refresh" in args for args in builds), (
            "without --refresh, Nix's fetch cache makes `always` last an hour"
        )

    async def test_if_not_present_reuses_a_floating_ref(
        self, monkeypatch, tmp_path
    ) -> None:
        builds: list = []
        await _mount(monkeypatch, tmp_path, FLOATING, PullPolicy.IF_NOT_PRESENT, builds)
        await _mount(monkeypatch, tmp_path, FLOATING, PullPolicy.IF_NOT_PRESENT, builds)
        assert len(builds) == 1

    async def test_a_collected_resolution_is_resolved_again(
        self, monkeypatch, tmp_path
    ) -> None:
        builds: list = []
        (first,) = await _mount(
            monkeypatch, tmp_path, LOCKED, PullPolicy.ALWAYS, builds
        )
        first.rmdir()  # what a GC on the node does
        await _mount(monkeypatch, tmp_path, LOCKED, PullPolicy.ALWAYS, builds)
        assert len(builds) == 2

    async def test_the_record_is_the_nodes(self, monkeypatch, tmp_path) -> None:
        builds: list = []
        await _mount(monkeypatch, tmp_path, LOCKED, PullPolicy.ALWAYS, builds)
        assert resolution.resolutions().get(LOCKED, SYSTEM) is not None
