import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("hubspoke", ROOT / "bin" / "syncthing-hubspoke.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

HUB, MAC, LAP = ("-".join([c * 7] * 8) for c in "ABC")
IDS = {"synchub": HUB, "macbook": MAC, "laptop": LAP}
ST = tool.ST

CONFIG = {
    "hub": {"name": "synchub", "ssh": "me@hub", "path": "/srv/sync/",
            "folders": ["Documents", "Photos", "WorkNotes"]},
    "clients": {
        "macbook": {"ssh": "local", "path": "~/Sync", "folders": ["Documents", "Photos"],
                    "paths": {"Photos": "~/Pictures"}, "id": MAC.replace("-", "").lower()},
        "laptop": {"ssh": "me@laptop", "path": "/home/me/Sync", "folders": ["Documents"]},
    },
}


def cfg(**changes):
    return tool.parse_config({**CONFIG, **changes})


def with_laptop(**changes):
    return cfg(clients={**CONFIG["clients"], "laptop": {**CONFIG["clients"]["laptop"], **changes}})


def state(me, folders=(), devices=()):
    return {
        "self_id": me,
        "config": {
            "folders": [{"id": fid, "label": label, "path": path, "type": "sendreceive",
                         "devices": [{"deviceID": d} for d in (me,) + tuple(devs)], **extra}
                        for fid, label, path, devs, extra in folders],
            "devices": [{"deviceID": d, "name": n, **flags} for d, n, flags in devices],
        },
        "connections": {},
        "pending_devices": {},
        "pending_folders": {},
    }


def tags(findings):
    return [f[0] for f in findings]


def raises(fn, text):
    try:
        fn()
    except tool.ConfigError as e:
        assert text in str(e), str(e)
    else:
        raise AssertionError("expected ConfigError containing %r" % text)


def test_parse_config():
    c = cfg()
    assert [m["name"] for m in c["machines"]] == ["synchub", "macbook", "laptop"]
    assert c["hub"]["folders"] == {"documents": "Documents", "photos": "Photos", "worknotes": "WorkNotes"}
    assert c["hub"]["path"] == "/srv/sync" and c["clients"][0]["id"] == MAC
    assert c["clients"][0]["paths"] == {"photos": "~/Pictures"}


def test_parse_rejects_mistakes():
    raises(lambda: with_laptop(id="nope"), "not a Syncthing device ID")
    raises(lambda: with_laptop(pth="/x"), "unknown key(s): pth")
    raises(lambda: with_laptop(folders=["Videos"]), "the hub does not have: Videos")
    raises(lambda: with_laptop(folders=["Documents", "documents"]), "listed twice")
    raises(lambda: with_laptop(path="Sync"), "absolute path")
    raises(lambda: with_laptop(paths={"Photos": "/x"}), "own folders")
    raises(lambda: with_laptop(id=MAC), "same device ID")
    raises(lambda: with_laptop(ssh="local"), "only one machine")
    raises(lambda: with_laptop(name="x"), "remove 'name'")
    raises(lambda: cfg(hub=None), "[hub] is missing")
    raises(lambda: tool.parse_config({**CONFIG, "Host": {}}), "unknown top-level key(s): Host")


def test_example_and_repo_configs_parse():
    if tool.tomllib is not None:
        tool.load_config(str(ROOT / "config.example.toml"))
        if (ROOT / "config.toml").exists():  # git-ignored; only present locally
            tool.load_config(str(ROOT / "config.toml"))


def test_fresh_hub_gets_everything():
    role, findings = tool.plan(state(HUB, devices=[(HUB, "box", {})]), cfg(), "synchub", IDS, "/home/me")
    assert role == "hub"
    assert set(tags(findings)) == {"SET", "ADD", "SHARE"}
    cmds = [c for f in findings for c in f[2]]
    assert ST + ["devices", "add", "--device-id", MAC, "--name", "macbook"] in cmds
    assert ["mkdir", "-p", "/srv/sync/WorkNotes"] in cmds
    shares = sorted((c[4], c[-1]) for c in cmds if c[-3:-1] == ["add", "--device-id"])
    assert shares == sorted([("documents", LAP), ("documents", MAC), ("photos", MAC)])


def test_client_paths_expand_with_that_machines_home():
    _, _, folders = tool.desired(cfg(), "macbook", IDS, "/Users/me")
    assert folders == {"documents": ("Documents", "/Users/me/Sync/Documents", [HUB]),
                       "photos": ("Photos", "/Users/me/Pictures", [HUB])}


def test_configured_client_has_nothing_to_do():
    s = state(LAP, folders=[("documents", "Documents", "/home/me/Sync/Documents", [HUB], {})],
              devices=[(LAP, "laptop", {}), (HUB, "synchub", {})])
    assert tool.plan(s, cfg(), "laptop", IDS, "/home/me") == ("client", [])


def test_legacy_mesh_client():
    s = state(MAC,
              folders=[("k3j9-x", "Docs", "/Users/me/Sync/Documents", [HUB, LAP], {"versioning": {"type": "simple"}}),
                       ("misc", "Misc", "/Users/me/Misc", [LAP], {})],
              devices=[(MAC, "macbook", {}), (HUB, "synchub", {"introducer": True}), (LAP, "laptop", {})])
    role, findings = tool.plan(s, cfg(), "macbook", IDS, "/Users/me")
    assert role == "client"
    assert sorted(tags(findings)) == ["ADD", "EXTRA", "INFO", "MIGRATE", "SET", "SHARE"]
    migrate = next(f for f in findings if f[0] == "MIGRATE")
    assert "versioning" in migrate[1] and "no longer shared with laptop" in migrate[1]
    assert migrate[2] == [ST + ["folders", "k3j9-x", "delete"],
                          ST + ["folders", "add", "--id", "documents", "--label", "Documents",
                                "--path", "/Users/me/Sync/Documents"],
                          ST + ["folders", "documents", "devices", "add", "--device-id", HUB]]
    extra = next(f for f in findings if f[0] == "EXTRA")
    assert extra[2] == [ST + ["folders", "misc", "devices", LAP, "delete"], ST + ["devices", LAP, "delete"]]
    # documents is migrated by hand, so apply only adds photos
    assert next(f for f in findings if f[0] == "ADD")[2][0] == ["mkdir", "-p", "/Users/me/Pictures"]


def test_path_taken_by_another_folder():
    s = state(LAP, folders=[("photos", "Photos", "/home/me/Sync/Documents", [], {})],
              devices=[(LAP, "laptop", {}), (HUB, "synchub", {})])
    findings = tool.plan(s, cfg(), "laptop", IDS, "/home/me")[1]
    assert "WARN" in tags(findings) and "ADD" not in tags(findings)


def test_tilde_paths_from_the_macos_app_match():
    s = state(LAP, folders=[("documents", "Documents", "~/Sync/Documents", [HUB], {}),
                            ("photos", "Photos", "~/Sync/Photos/", [], {})],
              devices=[(LAP, "laptop", {}), (HUB, "synchub", {})])
    c = with_laptop(path="~/Sync")
    assert tool.plan(s, c, "laptop", IDS, "/home/me") == ("client", [])
    # documents' directory is taken by another ID: never add a second folder on it
    s["config"]["folders"] = s["config"]["folders"][1:]
    s["config"]["folders"][0]["path"] = "~/Sync/Documents"
    assert "ADD" not in tags(tool.plan(s, c, "laptop", IDS, "/home/me")[1])


def test_duplicate_id_on_same_directory():
    s = state(LAP, folders=[("documents", "Documents", "/home/me/Sync/Documents/", [HUB], {}),
                            ("Documents", "Documents", "/home/me/Sync/Documents", [HUB], {})],
              devices=[(LAP, "laptop", {}), (HUB, "synchub", {})])
    findings = tool.plan(s, cfg(), "laptop", IDS, "/home/me")[1]
    assert findings == [("EXTRA", findings[0][1], [ST + ["folders", "Documents", "delete"]])]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
