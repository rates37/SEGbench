"""Container runtime layer.

Responsibility: a backend-agnostic protocol for creating, seeding, exec-ing in, and destroying the
per-run container, plus the LXD backend (primary, driven through the ``lxc`` CLI via
``subprocess``) and a podman fallback. Backend-specific concepts must not leak upward.

Implemented in phase 2.
"""
