from __future__ import annotations

import os

# Environment variables documented in `git`'s ENVIRONMENT VARIABLES section that override
# git's normal repository discovery. Any of these, if inherited from a parent process, make
# git silently ignore an explicit `-C <path>`/`cwd=` argument and operate on a different
# repository instead. Stripping them from every subsched-invoked git call is defense-in-depth
# hardening related to issue #147 (a leaked GIT_DIR was investigated there and not confirmed
# as the actual root cause of that incident, but this class of override is a real, documented
# git behavior worth foreclosing regardless).
GIT_LOCATION_OVERRIDE_VARS: frozenset[str] = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_CEILING_DIRECTORIES",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    }
)


def git_safe_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Return an environment safe to pass to a `git -C <path>`/`cwd=<path>` subprocess call.

    Strips variables that override git's repository discovery so an explicit path argument
    cannot be silently redirected to an unrelated repository by inherited environment state.
    Defaults to a copy of the current process environment when `base` is not given.
    """
    source = dict(os.environ) if base is None else dict(base)
    for name in GIT_LOCATION_OVERRIDE_VARS:
        source.pop(name, None)
    return source
