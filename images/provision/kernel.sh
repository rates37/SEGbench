#!/bin/bash
# Provisioning for the kernel overlay. Runs as root on top of a container launched from the base
# image; the packages in kernel.yaml are already installed.
set -euo pipefail

# `crash` is useless without knowing where it expects to find things, and an agent that has to
# discover that by trial and error burns its wall clock on it. Record the layout once, here.
cat >/etc/segbench-kernel-notes <<'EOF'
crash(8) is installed. There is no vmcore and no host kernel debug symbols in this container:
any dump or oops to analyse comes from the bug's attachments under /workspace.

  crash <vmlinux> <vmcore>     analyse a dump pair from /workspace
  perf                          from linux-tools-common; no kernel-version package is installed,
                                so perf can read recorded data but cannot record on this kernel
  pahole                        from dwarves; struct layout from DWARF
  cscope / ctags                index a kernel source tree
EOF

sed -i 's/^name=.*/name=kernel/' /etc/segbench-image
rm -rf /var/lib/apt/lists/*
