#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="$ROOT_DIR/vm-data"
ISO_NAME="ubuntu-26.04.1-desktop-amd64.iso"
ISO_URL="https://releases.ubuntu.com/26.04.1/$ISO_NAME"
SUMS_URL="https://releases.ubuntu.com/26.04.1/SHA256SUMS"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required to run the VM manager." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "The Docker daemon is unavailable or this user cannot access it." >&2
  exit 1
fi
if [[ ! -e /dev/kvm ]]; then
  echo "KVM is unavailable. This VM requires the /dev/kvm device." >&2
  exit 1
fi

mkdir -p "$DATA_DIR" "$ROOT_DIR/drop"
if [[ ! -f "$DATA_DIR/guac.env" ]]; then
  printf 'GUAC_JSON_SECRET=%s\n' "$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')" > "$DATA_DIR/guac.env"
  chmod 0600 "$DATA_DIR/guac.env"
fi

if [[ ! -f "$DATA_DIR/$ISO_NAME" ]]; then
  echo "Downloading the official Ubuntu 26.04.1 desktop ISO (about 6.5 GB)..."
  curl --fail --location --retry 3 --continue-at - "$ISO_URL" -o "$DATA_DIR/$ISO_NAME.part"
  mv "$DATA_DIR/$ISO_NAME.part" "$DATA_DIR/$ISO_NAME"
fi

SUMS_FILE="$DATA_DIR/$ISO_NAME.SHA256SUMS"
if [[ ! -f "$SUMS_FILE" ]]; then
  curl --fail --location --retry 3 "$SUMS_URL" -o "$SUMS_FILE.part"
  mv "$SUMS_FILE.part" "$SUMS_FILE"
fi

expected_sum="$(awk -v file="$ISO_NAME" '$2 == file || $2 == "*" file { print $1; found = 1 } END { if (!found) exit 1 }' "$SUMS_FILE")" || {
  echo "Could not find the ISO checksum in the official SHA256SUMS file." >&2
  exit 1
}
if [[ ! "$expected_sum" =~ ^[[:xdigit:]]{64}$ ]]; then
  echo "The official ISO checksum has an invalid format." >&2
  exit 1
fi
if ! (cd "$DATA_DIR" && printf '%s  %s\n' "$expected_sum" "$ISO_NAME" | sha256sum --check --status); then
  echo "ISO checksum verification failed; refusing to start the VM." >&2
  exit 1
fi

export KVM_GID
KVM_GID="$(stat -c '%g' /dev/kvm)"
docker compose --env-file "$DATA_DIR/guac.env" -f "$ROOT_DIR/docker-compose.yml" up -d --build

echo "Ubuntu session manager and Guacamole started on port 6080."
echo "The existing admin account and VM disk are preserved."
echo "Open port 6080 in VS Code or open vm.html."
