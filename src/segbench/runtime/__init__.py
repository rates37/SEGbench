"""Container runtime layer.

Responsibility: a backend-agnostic protocol for creating, seeding, exec-ing in, and destroying the
per-run container, plus the LXD backend (primary, driven through the ``lxc`` CLI via
``subprocess``) and a podman fallback. Backend-specific concepts must not leak upward.

Import :class:`Runtime` and the data types from here; import a concrete backend from its own
module, so that anything reaching for a specific one is visible in the import list.
"""

from segbench.runtime.base import (
    REGISTRY,
    UNPOLICED,
    BackendUnavailable,
    BackgroundProcess,
    CleanupRegistry,
    ContainerNotReady,
    ContainerSpec,
    ExecFailed,
    ExecResult,
    Handle,
    NetworkPolicy,
    ResourceLimits,
    Runtime,
    RuntimeFailure,
    UnsupportedOperation,
)

__all__ = [
    "REGISTRY",
    "UNPOLICED",
    "BackendUnavailable",
    "BackgroundProcess",
    "CleanupRegistry",
    "ContainerNotReady",
    "ContainerSpec",
    "ExecFailed",
    "ExecResult",
    "Handle",
    "NetworkPolicy",
    "ResourceLimits",
    "Runtime",
    "RuntimeFailure",
    "UnsupportedOperation",
]
