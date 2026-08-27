#!/bin/sh

set -eu

fail() {
  printf 'harnest installer: %s\n' "$*" >&2
  exit 1
}

need_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

download() {
  source_url=$1
  destination=$2
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    curl --fail --silent --show-error --location \
      --proto '=https' --tlsv1.2 \
      -H "Authorization: Bearer ${GITHUB_TOKEN}" \
      -H 'X-GitHub-Api-Version: 2022-11-28' \
      --output "$destination" "$source_url"
  else
    curl --fail --silent --show-error --location \
      --proto '=https' --tlsv1.2 \
      --output "$destination" "$source_url"
  fi
}

latest_tag() {
  latest_url="https://github.com/${repo}/releases/latest"
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    effective_url=$(curl --fail --silent --show-error --location \
      --proto '=https' --tlsv1.2 \
      -H "Authorization: Bearer ${GITHUB_TOKEN}" \
      -H 'X-GitHub-Api-Version: 2022-11-28' \
      --output /dev/null --write-out '%{url_effective}' "$latest_url")
  else
    effective_url=$(curl --fail --silent --show-error --location \
      --proto '=https' --tlsv1.2 \
      --output /dev/null --write-out '%{url_effective}' "$latest_url")
  fi
  resolved_tag=${effective_url##*/}
  [ -n "$resolved_tag" ] && [ "$resolved_tag" != latest ] || \
    fail "could not resolve the latest release for ${repo}"
  printf '%s\n' "$resolved_tag"
}

need_command curl
need_command mktemp
need_command tar
need_command grep
need_command awk

repo=${HARNEST_REPO:-creativeJoe007/harnest}
printf '%s\n' "$repo" | grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' || \
  fail 'HARNEST_REPO must use owner/repository form'

requested_version=${HARNEST_VERSION:-}
if [ -n "$requested_version" ]; then
  printf '%s\n' "$requested_version" | \
    grep -Eq '^v?[0-9A-Za-z][0-9A-Za-z._+-]*$' || \
    fail 'HARNEST_VERSION contains unsupported characters'
  case "$requested_version" in
    v*) release_tag=$requested_version ;;
    *) release_tag="v${requested_version}" ;;
  esac
else
  release_tag=$(latest_tag)
fi
version=${release_tag#v}

case $(uname -s) in
  Darwin) target_os=darwin ;;
  Linux) target_os=linux ;;
  *) fail "unsupported operating system: $(uname -s)" ;;
esac

case $(uname -m) in
  x86_64 | amd64) target_arch=amd64 ;;
  arm64 | aarch64) target_arch=arm64 ;;
  *) fail "unsupported architecture: $(uname -m)" ;;
esac

runtime_directory=${HARNEST_RUNTIME_DIR:-"${HOME}/.harnest/runtime"}
install_directory=${HARNEST_INSTALL_DIR:-"${HOME}/.local/bin"}
[ -n "$runtime_directory" ] || fail 'HARNEST_RUNTIME_DIR must not be empty'
[ -n "$install_directory" ] || fail 'HARNEST_INSTALL_DIR must not be empty'

case ${HARNEST_YES:-} in
  1) ;;
  '')
    if ! (: 2>/dev/null < /dev/tty); then
      fail 'confirmation requires a terminal; set HARNEST_YES=1 for non-interactive installation'
    fi
    printf 'Install Harnest %s?\n  CLI:     %s/harnest\n  Runtime: %s\nContinue? [y/N] ' \
      "$version" "$install_directory" "$runtime_directory" > /dev/tty
    IFS= read -r confirmation < /dev/tty || fail 'installation cancelled'
    case $confirmation in
      y | Y | yes | YES | Yes) ;;
      *) fail 'installation cancelled' ;;
    esac
    ;;
  *) fail 'HARNEST_YES must be 1 or unset' ;;
esac

archive="harnest_${version}_${target_os}_${target_arch}.tar.gz"
release_base="https://github.com/${repo}/releases/download/${release_tag}"
temporary_directory=$(mktemp -d "${TMPDIR:-/tmp}/harnest-install.XXXXXX")
trap 'rm -rf "$temporary_directory"' EXIT HUP INT TERM

download "${release_base}/${archive}" "${temporary_directory}/${archive}"
download "${release_base}/checksums.txt" "${temporary_directory}/checksums.txt"

expected_checksum=$(awk -v archive="$archive" \
  '$2 == archive || $2 == ("*" archive) { print $1; exit }' \
  "${temporary_directory}/checksums.txt")
[ -n "$expected_checksum" ] || fail "${archive} is absent from checksums.txt"

if command -v sha256sum >/dev/null 2>&1; then
  actual_checksum=$(sha256sum "${temporary_directory}/${archive}" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
  actual_checksum=$(shasum -a 256 "${temporary_directory}/${archive}" | awk '{print $1}')
else
  fail 'sha256sum or shasum is required for checksum verification'
fi
[ "$actual_checksum" = "$expected_checksum" ] || \
  fail "checksum verification failed for ${archive}"

extract_directory="${temporary_directory}/archive"
mkdir -p "$extract_directory"
tar -xzf "${temporary_directory}/${archive}" -C "$extract_directory"
[ -f "${extract_directory}/harnest" ] || fail 'release archive has no harnest binary'

chmod 0755 "${extract_directory}/harnest"
if [ -n "${HARNEST_BOOTSTRAP_PYTHON:-}" ]; then
  "${extract_directory}/harnest" runtime install \
    --bootstrap-python "$HARNEST_BOOTSTRAP_PYTHON" \
    --directory "$runtime_directory"
else
  "${extract_directory}/harnest" runtime install \
    --directory "$runtime_directory"
fi

mkdir -p "$install_directory"
binary_target="${install_directory}/harnest"
binary_temporary="${install_directory}/.harnest.install.$$"
trap 'rm -rf "$temporary_directory"; rm -f "$binary_temporary"' EXIT HUP INT TERM
cp "${extract_directory}/harnest" "$binary_temporary"
mv -f "$binary_temporary" "$binary_target"

printf 'Installed Harnest %s\n' "$version"
printf '  CLI:     %s\n' "$binary_target"
printf '  Runtime: %s\n' "$runtime_directory"
case ":${PATH}:" in
  *:"${install_directory}":*) ;;
  *) printf 'Add %s to PATH to run harnest.\n' "$install_directory" ;;
esac
