# SPDX-License-Identifier: MIT
"""Which store path a flake ref named as a mount source resolves to, per node.

Resolution follows OCI's image pull semantics:

- A **store path** is a digest: fetched if missing, never re-resolved.
- A **locked ref** (it names a revision or a NAR hash) is a digest too: every
  evaluation of it gives the same store path, so a node resolves it once and
  remembers the answer.
- A **floating ref** (a branch, a registry name like `nixpkgs#hello`) is a
  tag, and the pod's pull policy decides: `always` (the default, as for
  `:latest`) resolves it at every container start, refetching rather than
  trusting Nix's own fetch cache; `if-not-present` reuses what this node
  resolved before.

Nothing here coordinates across nodes; neither does OCI. Two nodes may
resolve a floating ref differently, which is why a pinned ref is better.
"""

import enum
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs

import structlog

from ..constants import CSI_ROOT

logger = structlog.get_logger("nixkube.nri.resolution")

_REV = re.compile(r"[0-9a-f]{40}")
# Flake ref types whose third path segment may be a revision:
# github:owner/repo/<rev-or-ref>.
_FORGES = ("github", "gitlab", "sourcehut")


class PullPolicyError(ValueError):
    """A pull policy annotation with a value that is not a policy."""


class PullPolicy(enum.Enum):
    ALWAYS = "always"
    IF_NOT_PRESENT = "if-not-present"

    @classmethod
    def parse(cls, value: str) -> "PullPolicy":
        try:
            return cls(value.strip().lower())
        except ValueError:
            choices = ", ".join(p.value for p in cls)
            raise PullPolicyError(
                f"unknown pull policy {value!r}, expected one of: {choices}"
            ) from None


def is_locked_ref(source: str) -> bool:
    """Whether a flake ref names one revision, so it always resolves the same.

    True when it carries `rev=` or `narHash=`, or a forge ref's revision
    segment is a full commit hash. Everything else, expressions included, is
    treated as floating: wrongly calling a ref floating costs an evaluation,
    wrongly calling it locked serves a stale path.
    """
    url = source.split("#", 1)[0]
    base, _, query = url.partition("?")
    params = parse_qs(query)
    if "rev" in params or "narHash" in params:
        return True
    scheme, _, rest = base.partition(":")
    if scheme in _FORGES:
        segments = rest.split("/")
        if len(segments) >= 3 and _REV.fullmatch(segments[2]):
            return True
    return False


class Resolutions:
    """What this node resolved each source to, kept across restarts.

    A JSON file of `"<system> <source>"` to store path. A remembered path
    that has since been collected is not returned, so it is resolved again.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text())
            except FileNotFoundError:
                self._data = {}
            except (OSError, ValueError):
                # Only a cache: start again rather than fail every mount.
                logger.warning("resolutions_unreadable", path=str(self.path))
                self._data = {}
        return self._data

    def get(self, source: str, system: str) -> Path | None:
        recorded = self._load().get(f"{system} {source}")
        if recorded is None:
            return None
        path = Path(recorded)
        return path if path.exists() else None

    def put(self, source: str, system: str, path: Path) -> None:
        data = {**self._load(), f"{system} {source}": str(path)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Written whole and renamed into place, so a crash leaves the old
        # file or the new one, never half of either. Remembered only once it
        # is written, so memory never claims what the file does not.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True))
        os.replace(tmp, self.path)
        self._data = data


_resolutions: Resolutions | None = None


def resolutions() -> Resolutions:
    """The node's record, under nixkube's state directory."""
    global _resolutions
    if _resolutions is None:
        _resolutions = Resolutions(CSI_ROOT / "resolutions.json")
    return _resolutions
