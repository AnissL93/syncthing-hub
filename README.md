# syncthing-hub

Run [Syncthing](https://syncthing.net) as a **hub and spoke**: one always-on hub holds every folder, and each client syncs only the folders it needs, through the hub. Clients never pair with each other.

You describe the layout in one TOML file. A script then sets up every machine over SSH.

```
 Client1 ──┐
           ├── Hub (all folders)
 Client2 ──┘
```

## Requirements

- Syncthing running on every machine, under the account you will SSH into.
- Key-based SSH from the machine you run the tool on to every other machine.
- Python 3.11 or newer on the machine you run the tool on. Older Python, including the `python3` built into macOS, needs `python3 -m pip install --user tomli` first.

## Quick start

1. Copy the example config and fill in your machines:

   ```bash
   cp config.example.toml config.toml
   ```

   ```toml
   [hub]
   name = "Host"
   ssh = "alice@100.64.0.3"
   path = "/srv/sync"
   folders = ["Documents", "Photos", "WorkNotes"]

   [clients.laptop]
   ssh = "local"            # the machine you run the tool on
   path = "~/Sync"
   folders = ["Documents", "Photos"]
   ```

   Each folder lives at `path/<folder name>`. A `paths = { Photos = "~/Pictures" }` entry moves an individual folder. The Syncthing folder ID is the folder name in lowercase (`WorkNotes` → `worknotes`), so don't rename a folder once machines are syncing. `config.toml` is git-ignored.

2. See what would change. This makes no changes:

   ```bash
   python3 bin/syncthing-hubspoke.py inspect config.toml
   ```

3. Apply it:

   ```bash
   python3 bin/syncthing-hubspoke.py apply config.toml
   python3 bin/syncthing-hubspoke.py apply config.toml laptop   # one machine only
   ```

The tool reads every machine's device ID over SSH, so you never copy IDs around. It sets up the hub first, then each client:

- pairs devices: the hub with every client, each client with the hub only;
- creates the folders and shares each one with the right devices;
- sets device names and folder labels, and turns off `introducer`.

`apply` only adds and updates, and running it again is safe. Anything that would remove or move something is printed with the machine to run it on, and is never run automatically. That covers extra devices and shares, folders that need a new ID, and duplicate folders on one directory.

## Moving an existing setup over

Machines that already sync (all paired with each other, random folder IDs, introducers) can be brought in place. Folders are matched to your config by ID, label or directory name. Run `inspect`, then `apply`, then the `MIGRATE` and `EXTRA` commands it prints, until every machine reports `Nothing to do`. Files stay on disk, and identical files are not re-transferred.

Have a backup first. Remove client-to-client links only after the hub has a full copy of the data.

## Files

| Path | What it is |
|---|---|
| `bin/syncthing-hubspoke.py` | The config-driven tool (`apply`, `inspect`) |
| `config.example.toml` | Commented config template |
| `bin/syncthing-hub.sh`, `bin/syncthing-client.sh` | Simpler scripts for setting up one machine by hand, for the five default folders only |
| `Syncthing-Headless-Hub-and-Spoke.md` | Full guide: installing Syncthing headless, running it at boot, firewall, troubleshooting, backups |
| `tests/test_hubspoke.py` | Tests for config parsing and planning |

## Tests

```bash
python3 -m pytest tests        # or: python3 tests/test_hubspoke.py
```
