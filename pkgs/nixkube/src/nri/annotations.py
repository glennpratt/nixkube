# SPDX-License-Identifier: MIT
"""NRI pod annotation parsing for Nix store mounts and RW /nix configuration."""

import re
from collections.abc import Iterator, Mapping
from pathlib import Path

from nri import nri_pb2

from ..store import extract_store_paths
from .policy import BuildPolicy
from .resolution import PullPolicy

# Annotations from protobuf are ScalarMap[str, str], which implements Mapping.
# Using Mapping keeps us compatible with both protobuf types and plain dicts (tests).
Annotations = Mapping[str, str]

# Where a store mount comes from. An absolute path is a store path, used as
# is. Anything else is a flake reference or a Nix expression for the build
# task to realise, and stays a string: `Path` would fold the `//` in
# `git+https://...` to `/`.
MountSource = Path | str


def _iter_annotations(
    annotations: Annotations, target: str, system: str
) -> Iterator[str]:
    """Yield annotation values for nixkube/{target}(@{system})?(-{index})? annotations.

    Annotations without @system apply to all systems. System-qualified annotations
    (@system) are only yielded when system matches.

    The optional -index suffix (always last) allows multiple annotations for the same
    target (e.g., nixkube/pod-1, nixkube/pod-2) and is ignored by the parser.
    """
    pattern = re.compile(rf"^nixkube/{re.escape(target)}(@{re.escape(system)}|)(-.*|)$")
    return (value for key, value in annotations.items() if pattern.fullmatch(key))


def extract_container_store_paths(
    req: nri_pb2.CreateContainerRequest, system: str
) -> set[Path]:
    """Extract Nix store paths from an NRI CreateContainerRequest.

    Scans container env, args, and pod annotations with nixkube/pod or
    nixkube/{container-name} prefixes. System-specific annotations (@{system})
    are only included when they match the current system; unqualified annotations
    apply to all systems.
    """
    annotation_values = [
        *_iter_annotations(req.pod.annotations, "pod", system),
        *_iter_annotations(req.pod.annotations, req.container.name, system),
    ]
    return extract_store_paths(
        [*req.container.env, *req.container.args, *annotation_values]
    )


def _parse_store_mounts_for_name(
    pod_annotations: Annotations, target_name: str, system: str
) -> dict[Path, MountSource]:
    """
    Parse store mount annotations matching a specific name (container name or "pod" for wildcard).

    Annotations format: nixkube/{target-name}(@{system})?(-{index})?: /path/in/container=/source
    - system: optional, filters annotation to specific system (e.g., x86_64-linux, aarch64-linux)
    - index: optional, allows multiple mounts per target (ignored by parser)
    - source: a store path (absolute), otherwise a flake reference or a Nix
      expression, kept verbatim and realised by the build task

    For wildcard (target_name="pod"):
      nixkube/pod-1: /etc/ssl/certs=/nix/store/cacert-1.0/etc/ssl/certs
      nixkube/pod@x86_64-linux-2: /etc/passwd=/nix/store/fakeNss-x86/etc/passwd

    For container (target_name="myapp"):
      nixkube/myapp@aarch64-linux-1: /etc/ssl=/nix/store/cacert-aarch64/etc/ssl

    Flake reference, realised on the node:
      nixkube/myapp: /app=github:NixOS/nixpkgs/<rev>#redis

    Returns: {Path("/path/in/container"): Path("/nix/store/.../package") or "flake#ref"}
    Annotations without @system apply to all systems.
    """
    return {
        Path(container_path): Path(source) if source.startswith("/") else source
        for value in _iter_annotations(pod_annotations, target_name, system)
        if "=" in value
        for container_path, source in [value.split("=", 1)]
    }


def parse_nix_rw(
    pod_annotations: Annotations, container_name: str, system: str
) -> bool:
    """Return True if this container should get a read-write /nix overlayfs.

    Container-specific annotation takes precedence over the pod-wide default,
    including an explicit "false" to opt a single container out of pod-wide RW.

    Supports system-specific variants with @{system} suffix.

    Annotations:
      nixkube/pod-rw: "true"                 — all containers get RW /nix
      nixkube/pod-rw@x86_64-linux: "true"   — all containers get RW /nix on x86_64
      nixkube/{container-name}-rw: "true"   — only this container gets RW /nix
      nixkube/{container-name}-rw@aarch64-linux: "false" — this container stays RO on aarch64
    """
    container_values = list(
        _iter_annotations(pod_annotations, f"{container_name}-rw", system)
    )
    if container_values:
        return any(v == "true" for v in container_values)
    return any(
        v == "true" for v in _iter_annotations(pod_annotations, "pod-rw", system)
    )


def parse_nix_exclude(
    pod_annotations: Annotations, container_name: str, system: str
) -> bool:
    """Return True if this container wants no injection at all.

    **For a container that brings its own store.** `CreateContainer` already
    stands down when `/nix` is mounted, and that check cannot serve a
    container whose store is somewhere else. `appstarter-init` is the case
    this exists for: it mounts the claim at `/nix-volume`, realises the
    closure into it as a chroot store, and must keep the image's own `/nix`
    visible, because that is where its fallback copy lives (issue #49). So it
    can neither mount `/nix` nor be injected into.

    Without an opt-out, NRI reads `APPSTARTER_WANTED` out of that container's
    environment and realises pynixd's closure into the *node's* store, whose
    only substituter is the pynixd this pod is starting. That is issue #27's
    cycle, and it wedged a cluster for a day. Issue #55.

    Same shape as `parse_nix_rw`: container-specific wins over pod-wide,
    including an explicit "false" to opt one container back in.

    Annotations:
      nixkube/pod-exclude: "true"                  — no container is injected into
      nixkube/{container-name}-exclude: "true"     — only this one is left alone
      nixkube/{container-name}-exclude@x86_64-linux: "false" — back in, on x86_64
    """
    container_values = list(
        _iter_annotations(pod_annotations, f"{container_name}-exclude", system)
    )
    if container_values:
        return any(v == "true" for v in container_values)
    return any(
        v == "true" for v in _iter_annotations(pod_annotations, "pod-exclude", system)
    )


def parse_build_policy(
    pod_annotations: Annotations, container_name: str, system: str
) -> BuildPolicy | None:
    """The build policy this container asks for, or None if it does not ask.

    Same shape as `parse_nix_rw`: a container-specific annotation wins over
    the pod-wide one. A request can only tighten the node's policy (see
    `effective_build_policy`), so if several annotations match, the strictest
    counts. An unknown value raises rather than being ignored: a pod that
    meant "pinned" and misspelt it must not get the node's default.

    Annotations:
      nixkube/pod-build: "substitute"                 — no container builds
      nixkube/{container-name}-build: "pinned"       — store paths only here
    """
    values = list(
        _iter_annotations(pod_annotations, f"{container_name}-build", system)
    ) or list(_iter_annotations(pod_annotations, "pod-build", system))
    if not values:
        return None
    return min(BuildPolicy.parse(v) for v in values)


def parse_pull_policy(
    pod_annotations: Annotations, container_name: str, system: str
) -> PullPolicy:
    """How this container's floating flake refs are resolved (see `resolution`).

    Same shape as `parse_build_policy`: the container's annotation wins over
    the pod's. `always` unless set, as for an image's `:latest`. An unknown
    value raises, like an unknown build policy.

    Annotations:
      nixkube/pod-pull: "if-not-present"
      nixkube/{container-name}-pull: "always"
    """
    values = list(
        _iter_annotations(pod_annotations, f"{container_name}-pull", system)
    ) or list(_iter_annotations(pod_annotations, "pod-pull", system))
    if not values:
        return PullPolicy.ALWAYS
    policies = [PullPolicy.parse(v) for v in values]
    # Several matching annotations: re-resolving is the safe disagreement.
    return PullPolicy.ALWAYS if PullPolicy.ALWAYS in policies else policies[0]


def parse_store_mounts(
    pod_annotations: Annotations, container_name: str, system: str
) -> dict[Path, MountSource]:
    """
    Parse store mount annotations from pod metadata with system filtering.

    Supports annotation patterns:
    1. Pod-wide (apply to all containers):  nixkube/pod: /etc/ssl=/source
    2. Container-specific (overrides pod-wide): nixkube/container-name: /etc/ssl=/source
    3. System-specific variants with @{system} suffix

    Source is auto-detected: store path, flake reference, or nix expression.

    Example annotations:
      nixkube/pod: /etc/ssl/certs=/nix/store/abc-cacert-1.0/etc/ssl/certs
      nixkube/pod@x86_64-linux: /etc/passwd=/nix/store/fakeNss-x86/etc/passwd
      nixkube/myapp@aarch64-linux-1: /etc/ssl=/nix/store/cacert-aarch64

    Returns dict: {Path("/path/in/container"): Path("/source")}
    - Container-specific annotations override pod-wide annotations for the same path
    - System-specific annotations apply only to matching system
    - Annotations without @system apply to all systems
    """
    # Container-specific overrides pod-wide for the same path
    return {
        **_parse_store_mounts_for_name(pod_annotations, "pod", system),
        **_parse_store_mounts_for_name(pod_annotations, container_name, system),
    }
