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

[[ $# -ge 4 ]] || {
    cat >&2 <<EOF
Usage:
  $0 SYNC_ROOT HUB_DEVICE_ID HUB_NAME FOLDER [FOLDER ...]

Folders:
  Archive Documents Photos WorkNotes WritingNotes
EOF
    exit 1
}

require_syncthing

SYNC_ROOT="${1%/}"
HUB_ID="$2"
HUB_NAME="$3"
shift 3
FOLDERS=("$@")

mkdir -p "$SYNC_ROOT"

if device_exists "$HUB_ID"; then
    echo "[OK] Hub device already configured: $HUB_NAME"
    syncthing cli config devices "$HUB_ID" name set "$HUB_NAME" >/dev/null || true
else
    syncthing cli config devices add \
        --device-id "$HUB_ID" \
        --name "$HUB_NAME"
    echo "[ADD] Hub device: $HUB_NAME"
fi

for name in "${FOLDERS[@]}"; do
    id="$(folder_id "$name")"
    path="$SYNC_ROOT/$name"

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

    if folder_has_device "$id" "$HUB_ID"; then
        echo "[OK] $name already shared back to hub"
    else
        syncthing cli config folders "$id" devices add \
            --device-id "$HUB_ID"
        echo "[ACCEPT] $name <-> $HUB_NAME"
    fi
done

echo
echo "Configuration complete."
echo
echo "Remaining pending folders:"
syncthing cli show pending folders
echo
echo "Connections:"
syncthing cli show connections
