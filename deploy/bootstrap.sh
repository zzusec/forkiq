#!/bin/sh
set -eu

# Run this from a checkout or an unpacked release.  The old bootstrap script
# created /opt/forkiq before looking for requirements.txt, which made a clean
# install fail unless the operator copied the application by hand first.
SOURCE_DIR=${1:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}
SOURCE_DIR=$(CDPATH= cd -- "$SOURCE_DIR" && pwd)
if test ! -f "$SOURCE_DIR/requirements.txt" || test ! -f "$SOURCE_DIR/app/main.py"; then
    printf 'Source directory is not a ForkIQ release: %s\n' "$SOURCE_DIR" >&2
    exit 1
fi

# ForkIQ initial provisioning; refuses to touch an existing installation.
for path in /opt/forkiq /etc/forkiq /var/lib/forkiq /etc/systemd/system/forkiq.service; do
    if test -e "$path"; then
        printf 'Refusing existing target: %s\n' "$path" >&2
        exit 1
    fi
done
if getent passwd forkiq >/dev/null; then
    printf 'Refusing an existing service account\n' >&2
    exit 1
fi
command -v python3 >/dev/null
command -v git >/dev/null
command -v systemctl >/dev/null

useradd --system --user-group --home-dir /var/lib/forkiq --shell /usr/sbin/nologin forkiq
install -d -o forkiq -g forkiq -m 0700 /var/lib/forkiq
install -d -o root -g root -m 0755 /opt/forkiq
install -d -o root -g forkiq -m 0750 /etc/forkiq

# Copy the application before creating the virtualenv.  Archive piping avoids
# requiring rsync and keeps the source checkout's .git directory out of the
# production tree.
tar -C "$SOURCE_DIR" --exclude=.git --exclude=.venv -cf - . | tar -C /opt/forkiq -xf -
install -m 0644 "$SOURCE_DIR/deploy/forkiq.service" /etc/systemd/system/forkiq.service
install -m 0640 -o root -g forkiq "$SOURCE_DIR/deploy/forkiq.env.example" /etc/forkiq/forkiq.env
# The App private key is created later by the manifest callback (0600, service user).
touch /var/lib/forkiq/app.pem
chown forkiq:forkiq /var/lib/forkiq/app.pem
chmod 0600 /var/lib/forkiq/app.pem

cd /opt/forkiq
python3 -m venv .venv
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt
chown -R root:root /opt/forkiq
chmod -R go-w /opt/forkiq
chown root:forkiq /etc/forkiq/forkiq.env
systemctl daemon-reload
systemctl enable forkiq.service

# The service binds loopback; no public or Docker bridge firewall rule is needed.
printf 'ForkIQ is installed. Edit /etc/forkiq/forkiq.env, configure HTTPS reverse proxy, then run: systemctl start forkiq.service\n'
