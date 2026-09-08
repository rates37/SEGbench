#!/bin/bash
# Provisioning for the segbench base image. Runs as root inside a throwaway container which is
# then stopped and published (see segbench.runtime.images).
#
# Packages named in base.yaml are already installed by the time this runs; this script covers what
# apt cannot do.
set -euo pipefail

AGENT_USER="${SEGBENCH_AGENT_USER:-agent}"

# `fd` is packaged as `fdfind` on Debian and Ubuntu because of a name clash. Every agent and every
# piece of documentation calls it `fd`, so give them `fd`.
if [ -x /usr/bin/fdfind ] && [ ! -e /usr/local/bin/fd ]; then
    ln -s /usr/bin/fdfind /usr/local/bin/fd
fi

# The unprivileged user the agent runs as. No sudo: the agent is diagnosing, not administering,
# and a run that can reconfigure its own container can undo the network policy that makes the
# benchmark valid.
if ! id -u "$AGENT_USER" >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash "$AGENT_USER"
fi

# /workspace is the agent's cwd and where it writes answer.json (plan.md §2).
mkdir -p /workspace
chown "$AGENT_USER:$AGENT_USER" /workspace

# opencode, the in-container agent (CLAUDE.md, technical decisions). The upstream installer drops
# a single binary into $OPENCODE_INSTALL_DIR; install it outside any user's home so the agent user
# gets it without a login shell, then symlink it onto PATH.
#
# The binary's exact filename is the installer's business and has changed before, so it is
# located rather than assumed, and the version call at the end is what actually proves the image
# is usable. A build that produces an image without a working opencode must fail here, not five
# minutes into the first run of a campaign.
# The installer's own destination has moved between releases and its install-dir environment
# variable is not reliably honoured, so let it install wherever it likes and then relocate the
# binary. Searching is dull but it does not break when upstream changes its mind.
curl -fsSL https://opencode.ai/install | bash

opencode_bin=$(find /root /opt /usr/local -maxdepth 4 -type f -name opencode 2>/dev/null | head -n1)
if [ -z "$opencode_bin" ]; then
    echo "the opencode installer did not produce a binary" >&2
    exit 1
fi
install -D -m 0755 -o root -g root "$opencode_bin" /opt/opencode/bin/opencode
rm -rf /root/.opencode
ln -sf /opt/opencode/bin/opencode /usr/local/bin/opencode

# Prove the image is usable now. A build that publishes an image with a broken opencode turns
# into a whole campaign of `harness_error` runs.
/usr/local/bin/opencode --version

# Nothing at runtime may reach a package index (plan.md §9). Removing the lists makes an
# accidental `apt-get install` inside a run fail immediately and visibly rather than hanging on a
# blocked proxy connection.
rm -rf /var/lib/apt/lists/*

# Record what this image is, so a container can be identified from the inside during debugging.
cat >/etc/segbench-image <<EOF
name=${SEGBENCH_IMAGE_NAME:-base}
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
agent_user=${AGENT_USER}
EOF
