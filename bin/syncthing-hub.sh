#!/usr/bin/env bash
set -euo pipefail

ALL_FOLDERS=(Archive Documents Photos WorkNotes WritingNotes)

die() {
    echo "ERROR: $*" >&2
    exit 1
}

folder_id() {
    case "$1" in
        Archive)      echo "archive" ;;
        Documents)    echo "documents" ;;
        Photos)       echo "photos" ;;
        WorkNotes)    echo "worknotes" ;;
        WritingNotes) echo "writingnotes" ;;
        *) die "Unknown folder '$1'. Allowed: ${ALL_FOLDERS[*]}" ;;
    esac
}

require_syncthing() {
    command -v syncthing >/dev/null 2>&1 || die "syncthing is not installed or not in PATH"
    syncthing cli show system >/dev/null 2>&1 ||
        die "Syncthing is not running for this user"
}

# Capture output before grepping: `cmd | grep -q` under pipefail can fail with
# SIGPIPE when grep exits early, which would look like "not found".
list_contains() {
    local needle="$1" out
    shift
    out="$("$@")" || die "Command failed: $*"
    grep -Fxq -- "$needle" <<<"$out"
}

device_exists() {
    list_contains "$1" syncthing cli config devices list
}

folder_exists() {
    list_contains "$1" syncthing cli config folders list
}

folder_has_device() {
    list_contains "$2" syncthing cli config folders "$1" devices list
}

# Syncthing happily configures two folder IDs on one directory, so check first.
folder_at_path() {
    local path="$1" ids id
    ids="$(syncthing cli config folders list)" || die "Command failed: syncthing cli config folders list"
    for id in $ids; do
        if [[ "$(syncthing cli config folders "$id" path get)" == "$path" ]]; then
            echo "$id"
            return
        fi
    done
}

ensure_folder() {
    local root="$1"
    local name="$2"
    local id path current_path other

    id="$(folder_id "$name")"
    path="${root%/}/$name"

    if folder_exists "$id"; then
        current_path="$(syncthing cli config folders "$id" path get)"
        syncthing cli config folders "$id" label set "$name" >/dev/null
        if [[ "$current_path" == "$path" ]]; then
            echo "[OK] $name ($id) -> $path"
        else
            echo "[KEEP] $name ($id) -> $current_path (not under $(dirname "$path"); left where it is)"
        fi
    else
        other="$(folder_at_path "$path")"
        [[ -z "$other" ]] ||
            die "Folder '$other' already uses '$path'. Run 'syncthing-hubspoke.py inspect' to migrate it to ID '$id'."
        mkdir -p "$path"
        syncthing cli config folders add \
            --id "$id" \
            --label "$name" \
            --path "$path"
        echo "[ADD] $name ($id) -> $path"
    fi
}

cmd_init() {
    [[ $# -eq 1 ]] || die "Usage: $0 init SYNC_ROOT"
    local root="${1%/}"

    mkdir -p "$root"

    for name in "${ALL_FOLDERS[@]}"; do
        ensure_folder "$root" "$name"
    done

    echo
    echo "Hub initialized."
    echo "Device ID:"
    syncthing device-id
}

cmd_add_client() {
    [[ $# -ge 3 ]] || die "Usage: $0 add-client DEVICE_ID DEVICE_NAME FOLDER [FOLDER ...]"

    local device_id="$1"
    local device_name="$2"
    shift 2

    if device_exists "$device_id"; then
        echo "[OK] Device already configured: $device_name"
        syncthing cli config devices "$device_id" name set "$device_name" >/dev/null || true
    else
        syncthing cli config devices add \
            --device-id "$device_id" \
            --name "$device_name"
        echo "[ADD] Device: $device_name"
    fi

    local name id
    for name in "$@"; do
        id="$(folder_id "$name")"

        folder_exists "$id" ||
            die "Hub folder '$id' is not configured. Run '$0 init /your/sync/root' first."

        if folder_has_device "$id" "$device_id"; then
            echo "[OK] $name already shared with $device_name"
        else
            syncthing cli config folders "$id" devices add \
                --device-id "$device_id"
            echo "[SHARE] $name -> $device_name"
        fi
    done

    echo
    echo "Client configured on hub."
}

cmd_status() {
    echo "Local device ID:"
    syncthing device-id
    echo
    echo "Devices:"
    syncthing cli config devices list
    echo
    echo "Folders:"
    syncthing cli config folders list
    echo
    echo "Connections:"
    syncthing cli show connections
}

require_syncthing

case "${1:-}" in
    init)
        shift
        cmd_init "$@"
        ;;
    add-client)
        shift
        cmd_add_client "$@"
        ;;
    status)
        shift
        cmd_status "$@"
        ;;
    *)
        cat >&2 <<EOF
Usage:
  $0 init SYNC_ROOT
  $0 add-client DEVICE_ID DEVICE_NAME FOLDER [FOLDER ...]
  $0 status

Folders:
  Archive Documents Photos WorkNotes WritingNotes
EOF
        exit 1
        ;;
esac
