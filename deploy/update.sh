#!/bin/sh
set -eu

# Atomic application upgrade for an existing bootstrap installation. State in
# /var/lib/forkiq and the deployment environment in /etc/forkiq are untouched.
SOURCE_DIR=${1:?"usage: deploy/update.sh /path/to/new-forkiq-release"}
SOURCE_DIR=$(CDPATH= cd -- "$SOURCE_DIR" && pwd)
if test ! -f "$SOURCE_DIR/requirements.txt" || test ! -f "$SOURCE_DIR/app/main.py"; then
    printf 'Source directory is not a ForkIQ release: %s\n' "$SOURCE_DIR" >&2
    exit 1
fi
if test ! -x /opt/forkiq/.venv/bin/pip || test ! -d /opt/forkiq; then
    printf 'No bootstrap installation found at /opt/forkiq\n' >&2
    exit 1
fi

stage=$(mktemp -d /opt/.forkiq-update.XXXXXX)
previous=/opt/forkiq.previous.$(date +%Y%m%d%H%M%S)
cleanup() { rm -rf "$stage"; }
trap cleanup EXIT

tar -C "$SOURCE_DIR" --exclude=.git --exclude=.venv -cf - . | tar -C "$stage" -xf -
systemctl stop forkiq.service
# Keep the old environment with the old release. Reusing it would make a
# dependency installation failure impossible to roll back reliably.
mv /opt/forkiq "$previous"
mv "$stage" /opt/forkiq
trap - EXIT

rollback() {
    systemctl stop forkiq.service || true
    rm -rf /opt/forkiq
    mv "$previous" /opt/forkiq
    systemctl start forkiq.service || true
    printf 'Upgrade failed; rolled back to %s\n' "$previous" >&2
}
if ! python3 -m venv /opt/forkiq/.venv ||
   ! /opt/forkiq/.venv/bin/pip install --quiet --disable-pip-version-check -r /opt/forkiq/requirements.txt; then
    rollback
    exit 1
fi
chown -R root:root /opt/forkiq
chmod -R go-w /opt/forkiq
systemctl daemon-reload
if ! systemctl start forkiq.service; then
    rollback
    exit 1
fi
printf 'ForkIQ upgraded. Previous release kept at %s\n' "$previous"
