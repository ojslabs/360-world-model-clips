#!/usr/bin/env bash
# Definitions precede the only invocation, so a truncated download cannot start setup.
wmc_fail() { printf 'World Model Clips: %s\n' "$*" >&2; exit 1; }

wmc_cleanup() {
  if [[ -n ${wmc_temp_dir:-} ]]; then rm -rf -- "$wmc_temp_dir"; fi
  if [[ -n ${wmc_lock_dir:-} ]]; then rmdir -- "$wmc_lock_dir" 2>/dev/null || true; fi
}

wmc_owned_resource() {
  local kind=$1 resource=$2
  [[ $(docker "$kind" inspect --format '{{ index .Labels "io.ojslabs.world-model-clips" }}' "$resource") == installer-v1 &&
     $(docker "$kind" inspect --format '{{ index .Labels "io.ojslabs.world-model-clips.install-dir" }}' "$resource") == "$wmc_install_dir" ]]
}

wmc_logs() {
  docker logs --tail 40 "$wmc_name" 2>&1 | sed -E \
    's/rk_[[:alnum:]_-]+/[Reactor credential hidden]/g; s/[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}:[[:xdigit:]]{32}/[Fal credential hidden]/g' >&2 || true
}

wmc_ready() {
  local attempt health login
  printf 'Waiting for the local editor…\n'
  for ((attempt=0; attempt<30; attempt++)); do
    health=$(curl --fail --silent --show-error --max-time 3 "$wmc_origin/healthz" 2>/dev/null) || health=''
    if [[ "$health" == *'"ok":true'* || "$health" == *'"ok": true'* ]]; then
      login=$(curl --fail --silent --show-error --max-time 3 "$wmc_origin/login" 2>/dev/null) || login=''
      if [[ "$login" == *'Sign in to World Model Clips'* ]]; then return; fi
    fi
    sleep 1
  done
  wmc_logs
  wmc_fail 'The container did not become ready. Its files and data are preserved; resolve the error and run this command again.'
}

wmc_validate_source() {
  local source=$1 file
  for file in Dockerfile bootstrap.py server.py requirements-linux.lock app/server.py config/orbit_preset.json ui/sections/00_editor.html; do
    [[ -f "$source/$file" && ! -L "$source/$file" ]] || wmc_fail "The source archive is missing $file."
  done
}

wmc_main() {
  set -euo pipefail
  local ref=main port=8476 name=world-model-clips install_dir="${HOME:?HOME is required}/.local/share/360-world-model-clips"
  local open_browser=1 port_supplied=0 arg value system marker config stored_name stored_port password volume image
  local existing=0 running prefix entry source container_owner container_directory actual_port
  while (($#)); do
    arg=$1; shift
    case "$arg" in
      --help|-h)
        printf '%s\n' 'Usage: install.sh [--ref main|40-character-commit] [--install-dir PATH] [--port PORT] [--name NAME] [--no-open]' \
          'Requires Bash, curl, tar and a running Docker engine. Python, Node.js and FFmpeg are installed inside the image.'
        return ;;
      --no-open) open_browser=0 ;;
      --ref|--install-dir|--port|--name)
        (($#)) || wmc_fail "$arg needs a value."
        value=$1; shift
        case "$arg" in
          --ref) ref=$value ;; --install-dir) install_dir=$value ;;
          --port) port=$value; port_supplied=1 ;; --name) name=$value ;;
        esac ;;
      *) wmc_fail "Unknown option: $arg. Use --help." ;;
    esac
  done
  [[ "$ref" == main || "$ref" =~ ^[[:xdigit:]]{40}$ ]] || wmc_fail '--ref must be main or a complete 40-character commit.'
  [[ "$port" =~ ^[0-9]{1,5}$ ]] && ((10#$port >= 1 && 10#$port <= 65535)) || wmc_fail '--port must be between 1 and 65535.'
  port=$((10#$port))
  [[ "$name" =~ ^[a-z0-9][a-z0-9_.-]{0,63}$ ]] || wmc_fail '--name must be 1 to 64 lowercase letters, numbers, dots, underscores or hyphens.'
  [[ -n "$install_dir" && "$install_dir" != *$'\n'* && "$install_dir" != *$'\r'* ]] || wmc_fail 'Choose an installation path without line breaks.'
  [[ "$install_dir" == /* ]] || install_dir="$PWD/$install_dir"
  [[ ! -L "$install_dir" ]] || wmc_fail 'The installation directory cannot be a symbolic link.'
  for value in curl tar gzip docker uname mktemp mkdir rmdir chmod cat sed tr od mv rm sleep; do
    command -v "$value" >/dev/null 2>&1 || wmc_fail "Missing $value. Install the prerequisites and run this command again."
  done
  system=$(uname -s)
  [[ "$system" == Darwin || "$system" == Linux ]] || wmc_fail 'This installer supports macOS and Linux with Docker.'
  docker info >/dev/null 2>&1 || wmc_fail 'Docker is not running or accessible. Start Docker Desktop or your Docker engine, then run this command again.'
  ref=$(printf '%s' "$ref" | tr 'A-F' 'a-f')
  umask 077
  wmc_temp_dir=''; wmc_lock_dir=''
  trap wmc_cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  if [[ -e "$install_dir" ]]; then
    [[ -d "$install_dir" && -f "$install_dir/.world-model-clips-install" && ! -L "$install_dir/.world-model-clips-install" &&
       $(cat "$install_dir/.world-model-clips-install") == world-model-clips-installer-v1 ]] ||
      wmc_fail 'The installation path already exists and is not owned by this installer. Choose a new --install-dir.'
  else
    mkdir -p -- "$install_dir"
    printf '%s\n' world-model-clips-installer-v1 > "$install_dir/.world-model-clips-install"
  fi
  wmc_install_dir=$(cd "$install_dir" && pwd -P)
  install_dir=$wmc_install_dir
  mkdir -- "$install_dir/.install-lock" 2>/dev/null || wmc_fail 'Another installer is running here. Wait for it to finish.'
  wmc_lock_dir="$install_dir/.install-lock"
  marker="$install_dir/.world-model-clips-install"
  config="$install_dir/config.env"
  if [[ -e "$config" || -L "$config" ]]; then
    [[ -f "$config" && ! -L "$config" && -f "$install_dir/container-name" && ! -L "$install_dir/container-name" ]] || wmc_fail 'The saved installation settings are invalid.'
    stored_name=$(cat "$install_dir/container-name")
    [[ "$stored_name" == "$name" ]] || wmc_fail "This directory belongs to container $stored_name. Use its original --name."
    password=$(sed -n 's/^DEMO_PASSWORD=//p' "$config")
    wmc_origin=$(sed -n 's/^PUBLIC_ORIGIN=//p' "$config")
    [[ "$password" =~ ^[a-f0-9]{48}$ && "$wmc_origin" =~ ^http://localhost:([0-9]{1,5})$ ]] || wmc_fail 'The saved access-code settings are invalid; they were not overwritten.'
    stored_port=${BASH_REMATCH[1]}
    ((stored_port >= 1 && stored_port <= 65535)) || wmc_fail 'The saved port is invalid.'
    [[ "$port_supplied" == 0 || "$port" == "$stored_port" ]] || wmc_fail "This installation uses port $stored_port. Use its original port."
    port=$stored_port
    [[ $(cat "$config") == "DEMO_PASSWORD=$password"$'\n'"PUBLIC_ORIGIN=$wmc_origin" ]] || wmc_fail 'The saved environment contains unexpected settings; it was not used.'
    chmod 600 "$config"
  else
    [[ ! -e "$install_dir/container-name" && ! -L "$install_dir/container-name" ]] || wmc_fail 'The installation has incomplete settings; no files were overwritten.'
    password=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
    [[ "$password" =~ ^[a-f0-9]{48}$ ]] || wmc_fail 'Could not create a random local access code.'
    wmc_origin="http://localhost:$port"
    printf 'DEMO_PASSWORD=%s\nPUBLIC_ORIGIN=%s\n' "$password" "$wmc_origin" > "$config"
    printf '%s\n' "$name" > "$install_dir/container-name"
    chmod 600 "$config"
  fi
  wmc_name=$name
  volume="$name-data"; image="$name:installed"
  if docker container inspect "$name" >/dev/null 2>&1; then
    container_owner=$(docker container inspect --format '{{ index .Config.Labels "io.ojslabs.world-model-clips" }}' "$name")
    container_directory=$(docker container inspect --format '{{ index .Config.Labels "io.ojslabs.world-model-clips.install-dir" }}' "$name")
    [[ "$container_owner" == installer-v1 && "$container_directory" == "$install_dir" ]] || wmc_fail "Container $name already exists and is not owned by this installation."
    actual_port=$(docker container inspect --format '{{range (index .HostConfig.PortBindings "8476/tcp")}}{{.HostIp}}:{{.HostPort}}{{end}}' "$name")
    [[ "$actual_port" == "127.0.0.1:$port" ]] || wmc_fail 'The existing container has a different port mapping; it was not changed.'
    running=$(docker container inspect --format '{{ .State.Running }}' "$name")
    printf 'Reusing the existing installation and saved media.\n'
    if [[ "$running" != true ]]; then
      docker start "$name" >/dev/null || { wmc_logs; wmc_fail 'Could not restart the existing container.'; }
    fi
  else
    if docker volume inspect "$volume" >/dev/null 2>&1; then
      wmc_owned_resource volume "$volume" || wmc_fail "Volume $volume already exists and is not owned by this installation."
    fi
    if docker image inspect "$image" >/dev/null 2>&1; then
      [[ $(docker image inspect --format '{{ index .Config.Labels "io.ojslabs.world-model-clips" }}' "$image") == installer-v1 &&
         $(docker image inspect --format '{{ index .Config.Labels "io.ojslabs.world-model-clips.install-dir" }}' "$image") == "$install_dir" ]] ||
        wmc_fail "Image $image already exists and is not owned by this installation."
    fi
    source="$install_dir/source"
    [[ ! -L "$source" ]] || wmc_fail 'The saved source directory cannot be a symbolic link.'
    if [[ ! -e "$source" ]]; then
      [[ ! -e "$install_dir/source-ref" && ! -L "$install_dir/source-ref" ]] || wmc_fail 'The saved source reference has no source directory; it was not overwritten.'
      printf 'Downloading the complete source archive (%s)…\n' "$ref"
      wmc_temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/world-model-clips.XXXXXX")
      curl --fail --location --silent --show-error --proto '=https' --connect-timeout 15 --max-time 300 \
        --output "$wmc_temp_dir/source.tar.gz" "https://codeload.github.com/ojslabs/360-world-model-clips/tar.gz/$ref" || wmc_fail 'Source download failed. Nothing was built or started.'
      tar -tzf "$wmc_temp_dir/source.tar.gz" > "$wmc_temp_dir/files" || wmc_fail 'The source archive is incomplete or unreadable.'
      tar -tvzf "$wmc_temp_dir/source.tar.gz" > "$wmc_temp_dir/types" || wmc_fail 'The source archive could not be inspected.'
      prefix="360-world-model-clips-$ref"
      while IFS= read -r entry; do
        [[ "$entry" == "$prefix/"* && "/$entry/" != *'/../'* && "$entry" != *'\'* ]] || wmc_fail 'The archive contains an unexpected path.'
      done < "$wmc_temp_dir/files"
      while IFS= read -r entry; do
        [[ "$entry" == -* || "$entry" == d* ]] || wmc_fail 'The archive contains links or special files.'
      done < "$wmc_temp_dir/types"
      mkdir "$wmc_temp_dir/extracted"
      tar -xzf "$wmc_temp_dir/source.tar.gz" -C "$wmc_temp_dir/extracted" --no-same-owner || wmc_fail 'Source extraction failed.'
      wmc_validate_source "$wmc_temp_dir/extracted/$prefix"
      mv -- "$wmc_temp_dir/extracted/$prefix" "$source"
      printf '%s\n' "$ref" > "$install_dir/source-ref"
    else
      [[ -d "$source" && ! -L "$source" ]] || wmc_fail 'The saved source directory is invalid.'
      wmc_validate_source "$source"
      printf 'Using the saved source without changing it.\n'
    fi
    printf 'Building Python, Node.js, FFmpeg and the editor inside Docker…\n'
    docker build --label io.ojslabs.world-model-clips=installer-v1 \
      --label "io.ojslabs.world-model-clips.install-dir=$install_dir" --tag "$image" "$source" || wmc_fail 'The image build failed. Source files are preserved for another attempt.'
    docker volume create --label io.ojslabs.world-model-clips=installer-v1 \
      --label "io.ojslabs.world-model-clips.install-dir=$install_dir" "$volume" >/dev/null
    printf 'Starting the editor on localhost…\n'
    docker run --detach --name "$name" --label io.ojslabs.world-model-clips=installer-v1 \
      --label "io.ojslabs.world-model-clips.install-dir=$install_dir" \
      --publish "127.0.0.1:$port:8476" --mount "source=$volume,target=/data" --env-file "$config" "$image" >/dev/null ||
      { wmc_logs; wmc_fail "The container could not start. Check whether port $port is already in use."; }
  fi
  wmc_ready
  printf '\nWorld Model Clips is ready: %s\nAccess code: %s\n' "$wmc_origin" "$password"
  printf 'Connect your own Fal key in the page. Reactor is optional.\nStop: docker stop %s\n' "$name"
  if [[ "$open_browser" == 1 ]]; then
    if [[ "$system" == Darwin ]] && command -v open >/dev/null 2>&1; then open "$wmc_origin" || true
    elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$wmc_origin" >/dev/null 2>&1 &
    fi
  fi
}

wmc_main "$@"
