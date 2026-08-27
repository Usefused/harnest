#!/bin/sh

set -eu

fail() {
  printf 'harnest installer: %s\n' "$*" >&2
  exit 1
}

need_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

is_legacy_python_launcher() {
  launcher_path=$1
  [ -f "$launcher_path" ] && grep -Eq \
    '^[[:space:]]*from[[:space:]]+harnest\.cli[[:space:]]+import[[:space:]]+main[[:space:]]*$' \
    "$launcher_path"
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
need_command dirname

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
existing_cli=$(command -v harnest 2>/dev/null || true)
if [ "${HARNEST_INSTALL_DIR+x}" = x ]; then
  install_directory=$HARNEST_INSTALL_DIR
else
  install_directory="${HOME}/.local/bin"
  if [ -n "$existing_cli" ] && is_legacy_python_launcher "$existing_cli"; then
    existing_directory=$(dirname "$existing_cli")
    if [ -w "$existing_directory" ]; then
      # Reusing the retired launcher's location makes the native CLI effective
      # immediately without silently rewriting the user's shell configuration.
      install_directory=$existing_directory
    fi
  fi
fi
[ -n "$runtime_directory" ] || fail 'HARNEST_RUNTIME_DIR must not be empty'
[ -n "$install_directory" ] || fail 'HARNEST_INSTALL_DIR must not be empty'
legacy_launcher_target=
if [ "$existing_cli" = "${install_directory}/harnest" ] && \
    is_legacy_python_launcher "$existing_cli"; then
  legacy_launcher_target=$existing_cli
fi

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
if [ -n "$legacy_launcher_target" ]; then
  printf '  Migrated: replaced the legacy Python launcher with the native CLI\n'
fi

resolved_cli=$(command -v harnest 2>/dev/null || true)
if [ "$resolved_cli" != "$binary_target" ]; then
  if [ -n "$resolved_cli" ]; then
    printf '\nWarning: harnest currently resolves to %s, not the native CLI above.\n' \
      "$resolved_cli"
    printf 'Place %s before %s on PATH before continuing.\n' \
      "$install_directory" "$(dirname "$resolved_cli")"
  else
    printf '\nAdd %s to PATH before continuing.\n' \
      "$install_directory"
  fi
  printf '  export PATH="%s:$PATH"\n' "$install_directory"
fi

printf '\nNext steps:\n'
printf '  harnest doctor\n'
printf '  harnest skills install\n'
printf '  harnest init my-agent --framework adk\n'
printf '  cd my-agent\n'
printf '  harnest test .\n'
printf '  harnest serve .\n'
printf '\nThe runtime at %s is managed internally by Harnest; do not activate or invoke it directly.\n' \
  "$runtime_directory"
