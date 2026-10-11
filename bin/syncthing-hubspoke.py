#!/usr/bin/env python3
"""Hub-and-spoke Syncthing setup driven by a TOML config file, applied over SSH.

Usage:
  syncthing-hubspoke.py apply CONFIG [MACHINE ...]    set machines up the way CONFIG describes
  syncthing-hubspoke.py inspect CONFIG [MACHINE ...]  show each machine's setup and what differs
  syncthing-hubspoke.py inspect                       show this machine's setup (no config needed)

Run it from any machine that can SSH to all the others. It reads each machine's
device ID itself. MACHINE limits the run to the named machines (default: all).
`apply` only adds and updates; anything that would remove a device, a share or a
folder is printed for you to run yourself.
"""
from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

# Used by `inspect` without a config file.
DEFAULT_FOLDERS = ["Archive", "Documents", "Photos", "WorkNotes", "WritingNotes"]
ST = ["syncthing", "cli", "config"]
DEVICE_ID = re.compile(r"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$")
AUTO_TAGS = ("ADD", "SET", "SHARE")
# Non-interactive SSH sessions often lack Homebrew's bin directories in PATH.
REMOTE_PATH = 'PATH="$PATH:/opt/homebrew/bin:/usr/local/bin" exec "$@"'
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/syncthing-hubspoke-%C", "-o", "ControlPersist=60"]


class ConfigError(Exception):
    pass


class MachineError(Exception):
    pass


# ---------------------------------------------------------------- config file

def folder_id(name: str) -> str:
    """WorkNotes -> worknotes, Active Reading -> active-reading."""
    return re.sub(r"\s+", "-", name.strip().lower())


def norm_device_id(value: object, where: str) -> str:
    raw = str(value).strip().upper().replace("-", "")
    dashed = "-".join(raw[i:i + 7] for i in range(0, len(raw), 7))
    if not DEVICE_ID.match(dashed):
        raise ConfigError("%s: '%s' is not a Syncthing device ID" % (where, value))
    return dashed


def parse_machine(name: str, table: object, where: str) -> dict:
    if not isinstance(table, dict):
        raise ConfigError("%s is missing or not a table" % where)
    extra = sorted(set(table) - {"name", "ssh", "id", "path", "folders", "paths"})
    if extra:
        raise ConfigError("%s: unknown key(s): %s" % (where, ", ".join(extra)))
    for key in ("ssh", "path", "folders"):
        if key not in table:
            raise ConfigError("%s: missing '%s'" % (where, key))
    folders = table["folders"]
    if not isinstance(folders, list) or not folders or not all(isinstance(f, str) and f for f in folders):
        raise ConfigError("%s: 'folders' must be a list of folder names" % where)
    bad = [f for f in folders if "/" in f]
    if bad:
        raise ConfigError("%s: folder names cannot contain '/': %s" % (where, ", ".join(bad)))
    ids = [folder_id(f) for f in folders]
    if len(set(ids)) != len(ids):
        raise ConfigError("%s: folder listed twice (names are case-insensitive)" % where)
    paths = table.get("paths", {})
    if not isinstance(paths, dict) or any(f not in folders for f in paths):
        raise ConfigError("%s: 'paths' may only override this machine's own folders" % where)
    for key, p in [("path", table["path"])] + [("paths.%s" % f, p) for f, p in paths.items()]:
        if not isinstance(p, str) or not (p.startswith("/") or p == "~" or p.startswith("~/")):
            raise ConfigError("%s: %s must be an absolute path or start with ~/" % (where, key))
    return {
        "name": name,
        "ssh": str(table["ssh"]),
        "id": norm_device_id(table["id"], where) if "id" in table else None,
        "path": table["path"].rstrip("/") or "/",
        "folders": {folder_id(f): f for f in folders},
        "paths": {folder_id(f): p for f, p in paths.items()},
    }


def parse_config(data: dict) -> dict:
    extra = sorted(set(data) - {"hub", "clients"})
    if extra:
        raise ConfigError("unknown top-level key(s): %s (expected [hub] and [clients.NAME])" % ", ".join(extra))
    hub_table = data.get("hub")
    hub = parse_machine(str((hub_table or {}).get("name", "hub")), hub_table, "[hub]")
    clients_table = data.get("clients", {})
    if not isinstance(clients_table, dict):
        raise ConfigError("write each client as its own [clients.NAME] table")
    clients = []
    for name, table in clients_table.items():
        if isinstance(table, dict) and "name" in table:
            raise ConfigError("[clients.%s]: the table name is the client's name; remove 'name'" % name)
        c = parse_machine(name, table, "[clients.%s]" % name)
        unknown = [c["folders"][f] for f in c["folders"] if f not in hub["folders"]]
        if unknown:
            raise ConfigError("[clients.%s]: folder(s) the hub does not have: %s" % (name, ", ".join(unknown)))
        clients.append(c)
    machines = [hub] + clients
    names = [m["name"] for m in machines]
    if len(set(names)) != len(names):
        raise ConfigError("a client has the same name as the hub")
    ids = [m["id"] for m in machines if m["id"]]
    if len(set(ids)) != len(ids):
        raise ConfigError("the same device ID is listed for two machines")
    if sum(m["ssh"] == "local" for m in machines) > 1:
        raise ConfigError("only one machine can be ssh = \"local\"")
    return {"hub": hub, "clients": clients, "machines": machines}


def load_config(path: str) -> dict:
    if tomllib is None:
        raise ConfigError("reading TOML needs Python 3.11+, or run: python3 -m pip install --user tomli")
    try:
        with open(path, "rb") as fh:
            return parse_config(tomllib.load(fh))
    except OSError as e:
        raise ConfigError("cannot read %s: %s" % (path, e.strerror))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError("%s is not valid TOML: %s" % (path, e))


def expand(path: str, home: str) -> str:
    return home.rstrip("/") + path[1:] if path == "~" or path.startswith("~/") else path


def desired(cfg: dict, me: str, ids: dict, home: str) -> tuple[str, dict, dict]:
    """(role, remote devices {id: name}, folders {id: (label, path, [device ids])}) for machine `me`."""
    hub, clients = cfg["hub"], cfg["clients"]
    entry = next(m for m in cfg["machines"] if m["name"] == me)

    def path(fid: str) -> str:
        return expand(entry["paths"].get(fid) or "%s/%s" % (entry["path"], entry["folders"][fid]), home)

    if me == hub["name"]:
        return "hub", {ids[c["name"]]: c["name"] for c in clients}, {
            fid: (label, path(fid), [ids[c["name"]] for c in clients if fid in c["folders"]])
            for fid, label in hub["folders"].items()}
    return "client", {ids[hub["name"]]: hub["name"]}, {
        fid: (label, path(fid), [ids[hub["name"]]]) for fid, label in entry["folders"].items()}


# ---------------------------------------------------------------- machines

class Machine:
    def __init__(self, name: str, ssh: str):
        self.name, self.ssh = name, ssh
        self.home = None

    def run(self, argv: list) -> subprocess.CompletedProcess:
        if self.ssh == "local":
            cmd = argv
        else:
            cmd = ["ssh", *SSH_OPTS, self.ssh, shlex.join(["sh", "-c", REMOTE_PATH, "sh", *argv])]
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            raise MachineError("%s: '%s' is not installed" % (self.name, cmd[0]))
        except subprocess.TimeoutExpired:
            raise MachineError("%s: '%s' timed out" % (self.name, shlex.join(argv)))

    def check(self, argv: list) -> str:
        res = self.run(argv)
        if res.returncode != 0:
            where = "" if self.ssh == "local" else " on %s" % self.ssh
            raise MachineError("%s: '%s' failed%s: %s" % (self.name, shlex.join(argv), where,
                                                         (res.stderr or res.stdout).strip() or "no output"))
        return res.stdout

    def load_state(self) -> dict:
        st = lambda *args: json.loads(self.check(["syncthing", "cli", *args]))
        self.home = str(Path.home()) if self.ssh == "local" else self.check(["printenv", "HOME"]).strip()
        return {
            "self_id": self.check(["syncthing", "device-id"]).strip(),
            "config": st("config", "dump-json"),
            "connections": st("show", "connections").get("connections") or {},
            "pending_devices": st("show", "pending", "devices") or {},
            "pending_folders": st("show", "pending", "folders") or {},
        }


# ---------------------------------------------------------------- planning

def match_folder(folder: dict, managed: dict) -> str | None:
    """The managed folder ID this folder corresponds to, matched by ID, label or directory name."""
    if folder["id"] in managed:
        return folder["id"]
    norm = lambda s: re.sub(r"[\s_-]", "", s.lower())
    keys = {norm(k): k for k in managed}
    keys.update({norm(v): k for k, v in managed.items()})
    for candidate in (folder.get("label") or "", folder["path"].rstrip("/").rsplit("/", 1)[-1]):
        if norm(candidate) in keys:
            return keys[norm(candidate)]
    return None


def plan(state: dict, cfg: dict | None = None, me: str | None = None, ids: dict | None = None,
         home: str = "") -> tuple[str, list]:
    """Compare a machine's live state with the config. Returns (role, findings); a finding is
    (tag, message, [argv, ...]). `apply` runs the commands of ADD/SET/SHARE findings only."""
    self_id = state["self_id"]
    current = state["config"]["folders"]
    by_id = {f["id"]: f for f in current}
    devices = {d["deviceID"]: d for d in state["config"]["devices"]}
    name = lambda d: (devices.get(d) or {}).get("name") or d[:7]
    remotes = lambda f: [d["deviceID"] for d in f["devices"] if d["deviceID"] != self_id]
    # Syncthing keeps paths as written, e.g. "~/Sync/Photos" from the macOS app.
    where = lambda f: expand(f["path"], home).rstrip("/") if home else f["path"].rstrip("/")
    managed = cfg["hub"]["folders"] if cfg else {folder_id(f): f for f in DEFAULT_FOLDERS}
    role, want_devices, want_folders = desired(cfg, me, ids, home) if cfg else ("unknown", {}, {})
    findings = []
    add = lambda tag, msg, *cmds: findings.append((tag, msg, list(cmds)))

    if cfg and (devices.get(self_id) or {}).get("name") != me:
        add("SET", "Name this device '%s'." % me, ST + ["devices", self_id, "name", "set", me])
    for dev_id, dev_name in want_devices.items():
        dev = devices.get(dev_id)
        if dev is None:
            add("ADD", "Pair with %s (%s)." % (dev_name, dev_id[:7]),
                ST + ["devices", "add", "--device-id", dev_id, "--name", dev_name])
        elif dev.get("name") != dev_name:
            add("SET", "Rename device %s to '%s'." % (dev_id[:7], dev_name),
                ST + ["devices", dev_id, "name", "set", dev_name])
    for dev_id, dev in devices.items():
        if dev_id != self_id and dev.get("introducer"):
            add("SET", "Turn off introducer for %s, so it cannot create client-to-client links." % name(dev_id),
                ST + ["devices", dev_id, "introducer", "set", "false"])

    # Folder IDs tie folders together across machines; find folders filed under another ID.
    migrating = {}
    claimed = {}
    for f in current:
        mid = match_folder(f, managed)
        if mid is None:
            add("INFO", "Folder '%s' (%s) at %s is not managed; leaving it alone."
                % (f["id"], f.get("label") or "no label", f["path"]))
            continue
        claimed.setdefault(mid, []).append(f["id"])
        if mid == f["id"]:
            continue
        if mid in by_id and where(by_id[mid]) == where(f):
            add("EXTRA", "Folder '%s' is a second folder ID on the same directory as '%s'. Remove it; the files "
                         "stay on disk and '%s' keeps syncing them." % (f["id"], mid, mid),
                ST + ["folders", f["id"], "delete"])
            continue
        if mid in by_id:
            add("WARN", "Folder '%s' at %s looks like %s, but folder ID '%s' already exists. "
                        "Decide which one to keep by hand." % (f["id"], f["path"], managed[mid], mid))
            continue
        if cfg and mid not in want_folders:
            add("INFO", "Folder '%s' looks like %s, which the config does not give this machine; leaving it alone."
                % (f["id"], managed[mid]))
            continue
        share = want_folders[mid][2] if cfg else remotes(f)
        cmds = [ST + ["folders", f["id"], "delete"],
                ST + ["folders", "add", "--id", mid, "--label", managed[mid], "--path", f["path"]]]
        if f.get("type", "sendreceive") != "sendreceive":
            cmds.append(ST + ["folders", mid, "type", "set", f["type"]])
        cmds += [ST + ["folders", mid, "devices", "add", "--device-id", d] for d in share]
        msg = ("Folder '%s' is %s under another ID. Re-create it as '%s' at the same path (files stay on disk; "
               "identical files are not re-transferred). Do the same on every device sharing it: %s."
               % (f["id"], managed[mid], mid, ", ".join(name(d) for d in remotes(f)) or "no other devices"))
        dropped = [name(d) for d in remotes(f) if d not in share]
        if dropped:
            msg += " Afterwards it is no longer shared with %s." % ", ".join(dropped)
        if (f.get("versioning") or {}).get("type"):
            msg += " Its file versioning settings are not carried over; set them again afterwards."
        migrating[f["id"]] = mid
        add("MIGRATE", msg, *cmds)
    for mid, owners in claimed.items():
        if len(owners) > 1 and mid not in owners:
            add("WARN", "Several folders look like %s: %s. Migrate only one of them."
                % (managed[mid], ", ".join(owners)))

    for fid, (label, path, share) in want_folders.items():
        f = by_id.get(fid)
        if f is None:
            if fid in migrating.values():
                continue
            occupant = next((o["id"] for o in current if where(o) == path), None)
            if occupant:
                add("WARN", "Cannot add %s: folder '%s' already uses %s." % (fid, occupant, path))
                continue
            add("ADD", "Folder %s at %s." % (label, path), ["mkdir", "-p", path],
                ST + ["folders", "add", "--id", fid, "--label", label, "--path", path])
            have = []
        else:
            if where(f) != path:
                add("KEEP", "Folder %s stays at %s (config says %s; move it by hand if you want)."
                    % (label, f["path"], path))
            if f.get("label") != label:
                add("SET", "Label folder %s '%s'." % (fid, label), ST + ["folders", fid, "label", "set", label])
            have = remotes(f)
            for d in have:
                if d not in share:
                    add("EXTRA", "Folder %s is shared with %s, which the config does not list for it."
                        % (label, name(d)), ST + ["folders", fid, "devices", d, "delete"])
        for d in share:
            if d not in have:
                add("SHARE", "Share %s with %s." % (label, want_devices.get(d) or name(d)),
                    ST + ["folders", fid, "devices", "add", "--device-id", d])

    if cfg:
        for dev_id in devices:
            if dev_id == self_id or dev_id in want_devices:
                continue
            shared = [f["id"] for f in current if dev_id in remotes(f) and f["id"] not in migrating]
            why = "not the hub" if role == "client" else "not in the config"
            add("EXTRA", "Paired with %s, which is %s%s. Remove it once the hub has a full copy of the data."
                % (name(dev_id), why, " (shares: %s)" % ", ".join(shared) if shared else ""),
                *[ST + ["folders", fid, "devices", dev_id, "delete"] for fid in shared],
                ST + ["devices", dev_id, "delete"])

    for dev_id, info in state["pending_devices"].items():
        if dev_id not in want_devices:
            hint = ("Clients only talk to the hub, so ignore it." if role == "client"
                    else "Add it under [clients] to accept it.")
            add("INFO", "Unknown device %s (%s) is trying to connect. %s"
                % (info.get("name") or dev_id[:7], dev_id, hint))
    for fid, info in state["pending_folders"].items():
        if fid not in want_folders:
            add("INFO", "Folder '%s' is offered by %s and not accepted."
                % (fid, ", ".join(name(d) for d in (info.get("offeredBy") or {}))))

    return role, findings


# ---------------------------------------------------------------- commands

def print_findings(findings: list, show_cmds: bool = True) -> None:
    for tag, msg, cmds in findings:
        print("  [%s] %s" % (tag, msg))
        if show_cmds:
            for cmd in cmds:
                print("      %s" % shlex.join(cmd))


def report(state: dict, role: str, findings: list, managed: dict) -> None:
    self_id = state["self_id"]
    devices = {d["deviceID"]: d for d in state["config"]["devices"]}
    name = lambda d: (devices.get(d) or {}).get("name") or d[:7]

    print("Device ID: %s" % self_id)
    print("Role:      %s" % role)
    print("\nDEVICES")
    for dev_id, dev in devices.items():
        if dev_id == self_id:
            continue
        c = state["connections"].get(dev_id) or {}
        status = "connected %s" % c.get("address", "") if c.get("connected") else "not connected"
        flags = [flag for flag in ("introducer", "paused") if dev.get(flag)]
        print("  %-20s %s  %s%s" % (name(dev_id), dev_id[:7], status, "  [%s]" % ", ".join(flags) if flags else ""))
    if len(devices) <= 1:
        print("  (none)")

    print("\nFOLDERS")
    for f in sorted(state["config"]["folders"], key=lambda f: f["id"]):
        shared = [name(d["deviceID"]) for d in f["devices"] if d["deviceID"] != self_id]
        mid = match_folder(f, managed)
        tag = "managed" if mid == f["id"] else ("-> %s" % mid if mid else "unmanaged")
        print("  %-14s %-14s %-12s %s" % (f["id"], f.get("label", ""), f.get("type", ""), f["path"]))
        print("  %-14s shared with: %s  [%s]" % ("", ", ".join(shared) or "(nobody)", tag))
    if not state["config"]["folders"]:
        print("  (none)")

    print("\nFINDINGS")
    if not [f for f in findings if f[0] not in ("INFO", "KEEP")]:
        print("  Nothing to do.")
    print_findings(findings)


def execute(machine: Machine, findings: list) -> None:
    done = 0
    for tag, msg, cmds in findings:
        if tag not in AUTO_TAGS:
            continue
        for cmd in cmds:
            machine.check(cmd)
        print("  [%s] %s" % (tag, msg))
        done += 1
    if not done:
        print("  Already up to date.")
    manual = [f for f in findings if f[0] not in AUTO_TAGS]
    if manual:
        print("\n  Not changed automatically (run these on %s yourself):" % machine.name)
        print_findings(manual)


def device_ids(cfg: dict, machines: dict) -> dict:
    """Ask every machine for its device ID; fall back to `id` from the config for unreachable ones."""
    ids, errors = {}, []
    for m in cfg["machines"]:
        try:
            found = machines[m["name"]].check(["syncthing", "device-id"]).strip()
        except MachineError as e:
            if m["id"]:
                print("note: %s is unreachable; using the id from the config (%s)" % (m["name"], e))
                ids[m["name"]] = m["id"]
            else:
                errors.append("%s (add `id = \"...\"` to its config entry if it is offline)" % e)
            continue
        if m["id"] and m["id"] != found:
            errors.append("%s: config id %s does not match the device at %s (%s)"
                          % (m["name"], m["id"][:7], m["ssh"], found[:7]))
        ids[m["name"]] = found
    if errors:
        raise MachineError("cannot read every device ID:\n  " + "\n  ".join(errors))
    return ids


def run_with_config(command: str, cfg: dict, only: list) -> int:
    names = [m["name"] for m in cfg["machines"]]
    unknown = [n for n in only if n not in names]
    if unknown:
        print("ERROR: no machine named %s in the config (have: %s)" % (", ".join(unknown), ", ".join(names)),
              file=sys.stderr)
        return 2
    machines = {m["name"]: Machine(m["name"], m["ssh"]) for m in cfg["machines"]}
    try:
        ids = device_ids(cfg, machines)
    except MachineError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1
    failed = []
    for m in cfg["machines"]:  # hub first
        if only and m["name"] not in only:
            continue
        print("\n==== %s (%s) ====" % (m["name"], m["ssh"]))
        machine = machines[m["name"]]
        try:
            state = machine.load_state()
            role, findings = plan(state, cfg, m["name"], ids, machine.home)
            if command == "apply":
                execute(machine, findings)
            else:
                report(state, role, findings, cfg["hub"]["folders"])
        except MachineError as e:
            print("ERROR: %s" % e)
            failed.append(m["name"])
    if command == "inspect":
        print("\n`apply` runs the ADD/SET/SHARE steps. Other commands are never run automatically.")
    if failed:
        print("\nFailed on: %s" % ", ".join(failed), file=sys.stderr)
    return 1 if failed else 0


def main(argv: list) -> int:
    if len(argv) < 2 or argv[1] not in ("apply", "inspect") or (argv[1] == "apply" and len(argv) < 3):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if len(argv) == 2:  # inspect this machine without a config
        machine = Machine("this machine", "local")
        try:
            state = machine.load_state()
        except MachineError as e:
            print("ERROR: %s" % e, file=sys.stderr)
            return 1
        role, findings = plan(state)
        report(state, "unknown (pass a config file to compare against it)", findings,
               {folder_id(f): f for f in DEFAULT_FOLDERS})
        return 0
    try:
        cfg = load_config(argv[2])
    except ConfigError as e:
        print("ERROR: config: %s" % e, file=sys.stderr)
        return 1
    return run_with_config(argv[1], cfg, argv[3:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
