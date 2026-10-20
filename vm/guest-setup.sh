#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script with sudo: sudo /mnt/host-setup/guest-setup.sh" >&2
  exit 1
fi

guest_user="${SUDO_USER:-}"
if [[ -z "$guest_user" || "$guest_user" == "root" ]]; then
  echo "Run this script with sudo from your Ubuntu desktop user account." >&2
  exit 1
fi

guest_home="$(getent passwd "$guest_user" | cut -d: -f6)"
if [[ -z "$guest_home" || ! -d "$guest_home" ]]; then
  echo "Could not find the home directory for $guest_user." >&2
  exit 1
fi

apt-get update
apt-get install -y --no-install-recommends curl jq

java_feature="$(curl --fail --silent --show-error --location \
  https://api.adoptium.net/v3/info/available_releases | jq -er '.most_recent_feature_release')"
if [[ ! "$java_feature" =~ ^[0-9]+$ ]]; then
  echo "Adoptium returned an invalid Java feature release." >&2
  exit 1
fi

tmp_java="$(mktemp /tmp/temurin-jre.XXXXXX.tar.gz)"
trap 'rm -f "$tmp_java"' EXIT
curl --fail --location --retry 3 \
  "https://api.adoptium.net/v3/binary/latest/${java_feature}/ga/linux/x64/jre/hotspot/normal/eclipse" \
  -o "$tmp_java"
install -d -m 0755 /opt/temurin-jre
tar -xzf "$tmp_java" --strip-components=1 -C /opt/temurin-jre
update-alternatives --install /usr/bin/java java /opt/temurin-jre/bin/java 20000
update-alternatives --set java /opt/temurin-jre/bin/java
printf 'export JAVA_HOME=/opt/temurin-jre\n' > /etc/profile.d/temurin-jre.sh

install -d -m 0755 /mnt/host-drop /mnt/host-control
if ! grep -qE '^[[:space:]]*hostdrop[[:space:]]' /etc/fstab; then
  printf 'hostdrop /mnt/host-drop 9p ro,trans=virtio,version=9p2000.L,nofail 0 0\n' >> /etc/fstab
fi
if ! grep -qE '^[[:space:]]*hostcontrol[[:space:]]' /etc/fstab; then
  printf 'hostcontrol /mnt/host-control 9p trans=virtio,version=9p2000.L,nofail 0 0\n' >> /etc/fstab
fi
mountpoint -q /mnt/host-drop || mount /mnt/host-drop
mountpoint -q /mnt/host-control || mount /mnt/host-control

cat > /usr/local/bin/import-host-downloads <<'EOF'
#!/bin/sh
set -eu
source_dir=/mnt/host-drop
target_dir="${HOME}/Downloads"

if ! mountpoint -q "$source_dir"; then
  echo "The host drop folder is not mounted; skipping the import." >&2
  exit 0
fi

mkdir -p "$target_dir"
cp -an "$source_dir/." "$target_dir/"
EOF
chmod 0755 /usr/local/bin/import-host-downloads

cat > /usr/local/bin/watch-ubuntu-logout <<'EOF'
#!/bin/sh
set -eu
seen_session=0
while :; do
  if pgrep -u "$GUEST_USER" -x gnome-session-binary >/dev/null 2>&1; then
    seen_session=1
  elif [ "$seen_session" -eq 1 ]; then
    touch /mnt/host-control/logout
    seen_session=0
  fi
  sleep 2
done
EOF
chmod 0755 /usr/local/bin/watch-ubuntu-logout

cat > /usr/local/bin/watch-host-downloads <<'EOF'
#!/bin/sh
set -eu
while :; do
  if mountpoint -q /mnt/host-drop; then
    target_dir="$GUEST_HOME/Downloads"
    install -d -o "$GUEST_USER" -g "$GUEST_USER" "$target_dir"
    cp -an /mnt/host-drop/. "$target_dir/"
  fi
  sleep 15
done
EOF
chmod 0755 /usr/local/bin/watch-host-downloads

cat > /etc/systemd/system/watch-ubuntu-logout.service <<EOF
[Unit]
Description=Pause this VM when its Ubuntu desktop logs out
After=local-fs.target display-manager.service

[Service]
Type=simple
Environment=GUEST_USER=$guest_user
ExecStart=/usr/local/bin/watch-ubuntu-logout
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
systemctl enable watch-ubuntu-logout.service

cat > /etc/systemd/system/watch-host-downloads.service <<EOF
[Unit]
Description=Copy new files from the host drop folder
After=local-fs.target

[Service]
Type=simple
Environment=GUEST_USER=$guest_user
Environment=GUEST_HOME=$guest_home
ExecStart=/usr/local/bin/watch-host-downloads
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF
systemctl enable watch-host-downloads.service

touch /mnt/host-control/ubuntu-installed

cat > /usr/local/bin/sklauncher <<'EOF'
#!/bin/sh
set -eu

jar="$(find "$HOME/Downloads" -maxdepth 1 -type f -iname 'sklauncher*.jar' -print -quit)"
if [ -z "$jar" ]; then
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --title="SKlauncher" --text="Put the SKlauncher JAR in Downloads (or the host drop folder) and try again."
  else
    echo "Put the SKlauncher JAR in Downloads (or the host drop folder) and try again." >&2
  fi
  exit 1
fi

exec /usr/bin/java -XX:+UseG1GC -jar "$jar"
EOF
chmod 0755 /usr/local/bin/sklauncher

cat > /usr/share/applications/sklauncher.desktop <<'EOF'
[Desktop Entry]
Name=SKlauncher
Comment=Launch SKlauncher from Downloads
Exec=sklauncher
Terminal=false
Type=Application
Categories=Game;
EOF
chmod 0644 /usr/share/applications/sklauncher.desktop

echo "Setup complete. Java $java_feature is installed. Reboot Ubuntu to activate the logout watcher."
