# SPDX-License-Identifier: MIT
"""How much work a container may make the node do to realise its store.

A development cluster wants `nixkube/redis: /app=github:me/app#redis` to just
work, evaluation and builds included. A production cluster wants the opposite:
nothing gets onto a node unless it is a store path that is already in a cache,
so a pod can neither run a build on the node nor start with something nobody
pinned.

The node sets the ceiling (`NRI_BUILD_POLICY`). A pod may ask for less with
`nixkube/pod-build` or `nixkube/{container}-build`, and never for more: the
annotation is written by whoever writes the pod, so it cannot be what lets a
build onto a node that forbids them.
"""

import enum

from ..constants import NRI_BUILD_POLICY


class BuildPolicyError(ValueError):
    """A container asked for something its build policy does not allow."""


class BuildPolicy(enum.IntEnum):
    """Ordered from least to most permissive, so `min` is "the stricter"."""

    # Literal store paths only, fetched from substituters. No evaluation.
    PINNED = 0
    # Flake refs are evaluated and their outputs fetched. Nothing is built.
    SUBSTITUTE = 1
    # Evaluate and build, locally or on the configured builders.
    BUILD = 2

    @classmethod
    def parse(cls, value: str) -> "BuildPolicy":
        try:
            return cls[value.strip().upper()]
        except KeyError:
            choices = ", ".join(p.name.lower() for p in cls)
            raise BuildPolicyError(
                f"unknown build policy {value!r}, expected one of: {choices}"
            ) from None


def node_build_policy() -> BuildPolicy:
    return BuildPolicy.parse(NRI_BUILD_POLICY)


def effective_build_policy(
    node: BuildPolicy, requested: BuildPolicy | None
) -> BuildPolicy:
    """The node's policy, tightened by the pod's request if it made one."""
    return node if requested is None else min(node, requested)


def nix_policy_args(policy: BuildPolicy) -> list[str]:
    """Arguments that make `nix build` honour the policy.

    Appended after the builder and substituter arguments, so they win: with
    no local jobs and no builders, anything that is not in a substituter
    fails with "no substituter can build it" instead of being built.
    """
    if policy is BuildPolicy.BUILD:
        return []
    return ["--max-jobs", "0", "--builders", ""]
