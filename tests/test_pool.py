from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fixture
import testlog
from dashboard import board as board_module
from dashboard import reader
from dashboard import search as search_module
from dashboard.app import Router
from dashboard.fleet import (Fleet, WORKERS, attached_ceiling, key_group,
                             read_only_uri)

ROOT = Path(__file__).resolve().parent.parent
DATA = testlog.work_dir("pool")

WHEN = 1_790_000_000_000

failures: list[str] = []

def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  [PASS] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}" + (f" -- {detail}" if detail else ""))

def build(root: Path, count: int) -> dict:
    root = Path(root)
    projects_root = root / "projects"
    if projects_root.exists():
        shutil.rmtree(projects_root, ignore_errors=True)
    projects_root.mkdir(parents=True, exist_ok=True)
    writers: list[sqlite3.Connection] = []
    threads: dict[str, str] = {}
    directories: dict[str, Path] = {}
    for index in range(count):
        label = f"Pool{index:02d}"
        ident = str(uuid.uuid5(uuid.NAMESPACE_URL, label))
        directory = projects_root / f"{label}-{ident}"
        directory.mkdir(parents=True, exist_ok=True)
        meta = {"version": 1, "projectId": ident,
                "projectPath": f"C:/work/{label}", "database": "desktop-v2.db"}
        (directory / "project.json").write_text(
            json.dumps(meta), encoding="utf-8", newline="\n")
        connection = sqlite3.connect(directory / "desktop-v2.db")
        connection.executescript(fixture.SCHEMA)
        connection.execute("pragma journal_mode=wal")
        connection.execute("pragma wal_autocheckpoint=0")
        connection.execute("insert into projects (id, path, created_at)"
                           " values (?,?,?)", (ident, meta["projectPath"], WHEN))
        thread_id = f"pool-thread-{index:02d}"
        state = "working" if index == 0 else "idle"
        connection.execute(
            "insert into threads (id, project_id, project_path, title, status,"
            " turn_state, model, created_at, updated_at, sidebar_archived_at)"
            " values (?,?,?,?,?,?,?,?,?,NULL)",
            (thread_id, ident, meta["projectPath"], f"{label} Thread", "open",
             state, "claude", WHEN, WHEN))
        connection.execute(
            "insert into messages (thread_id, role, parts_json, attachments_json,"
            " metrics_json, ts) values (?,?,?,?,?,?)",
            (thread_id, "user",
             json.dumps([{"kind": "text", "text": f"pool needle {index}"}]),
             "[]", "{}", WHEN))
        connection.commit()
        writers.append(connection)
        threads[label] = thread_id
        directories[label] = directory
    return {"writers": writers, "threads": threads, "directories": directories}

def set_database(directory: Path, name: str) -> None:
    path = directory / "project.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["database"] = name
    path.write_text(json.dumps(meta), encoding="utf-8", newline="\n")

def messages_in(fleet: Fleet, project) -> int:
    return fleet.read(project, lambda connection, key=project.key:
                      connection.execute(
                          f"select count(*) from {key}.messages").fetchone()[0])

def connection_for(fleet: Fleet, project):
    return fleet.read(project, lambda connection: connection)

def main() -> int:
    print("=== the pool: more projects than one connection can attach ===")
    ceiling = attached_ceiling()
    count = ceiling + 2
    print(f"      probed ceiling {ceiling}; building {count} project(s)")
    facts = build(DATA, count)
    config = fixture.config_for(DATA)
    fleet = Fleet(config)
    try:
        check("more projects than one connection's ceiling are all discovered",
              len(fleet.projects) == count, f"{len(fleet.projects)} != {count}")
        unreadable = [f"{project.label}: {project.unreadable}"
                      for project in fleet.projects if not project.readable]
        check("every one of them is read", not unreadable, "; ".join(unreadable))
        check("the ceiling the pool chunks by is this interpreter's own",
              fleet.ceiling == ceiling, f"{fleet.ceiling} != {ceiling}")
        check("more projects than one connection can attach take more than one "
              "group", len(fleet.groups()) >= 2, f"{len(fleet.groups())} group(s)")
        check("no group asks a connection for more than the ceiling",
              all(len(members) <= fleet.ceiling
                  for members in fleet.plan.values()),
              "; ".join(f"{group}: {len(members)}"
                        for group, members in fleet.plan.items()))
        check("a project's group is derived from its own key",
              all(fleet.group_of(project) == key_group(project.key, fleet.ceiling)
                  for project in fleet.projects))

        ordered = sorted(fleet.projects,
                         key=lambda project: int(project.key[1:]))
        first, last = ordered[0], ordered[-1]
        check("the far project sits in a different group than the first",
              fleet.group_of(first) != fleet.group_of(last),
              f"{first.key}/{fleet.group_of(first)} "
              f"{last.key}/{fleet.group_of(last)}")
        check("a project in the first group answers",
              messages_in(fleet, first) == 1)
        check("a project in the far group answers too",
              messages_in(fleet, last) == 1)

        flat = sqlite3.connect(":memory:", uri=True)
        refused = None
        try:
            for project in fleet.projects:
                flat.execute(
                    f"attach database '{read_only_uri(project.database)}'"
                    f" as {project.key}")
        except sqlite3.Error as exc:
            refused = exc
        finally:
            flat.close()
        check("one flat connection attaching them all hits the ceiling -- the "
              "defect this pool exists to remove",
              refused is not None and "too many attached" in str(refused),
              str(refused))

        router = Router(fleet, config)
        tree = router.projects()
        check("/api/projects lists every project",
              len(tree["projects"]) == count, str(len(tree["projects"])))
        check("/api/projects marks every one readable",
              all(row["unreadable"] is None for row in tree["projects"]))
        status = router.status()
        check("/api/status reports the probed ceiling beside the capabilities",
              status["sqlite"]["attached_limit"] == ceiling,
              str(status["sqlite"].get("attached_limit")))
        check("/api/status reports how many groups the fleet needed",
              status["sqlite"]["attach_groups"] == len(fleet.groups()),
              f"{status['sqlite'].get('attach_groups')} != {len(fleet.groups())}")
        check("/api/status asks every project for its own maximum sequence",
              all(row["max_seq"] == 1 for row in status["projects"]),
              str([row["max_seq"] for row in status["projects"]]))

        answer = search_module.search(fleet, search_module.Filters(
            q="pool needle", mode="words", scope="all"))
        check("a fleet-wide search reaches every group",
              answer["total"] == count, f"{answer['total']} != {count}")
        found = reader.thread(fleet, last.label, last.threads[0].id)
        check("a thread in the far group renders",
              found["title"] == f"{last.label} Thread"
              and len(found["turns"]) == 1, json.dumps(found)[:160])
        board = board_module.running(fleet)
        check("the activity board finds the turn in flight",
              board["running"] == 1
              and any(row["project"] == first.label
                      for row in board["threads"]),
              str([row["project"] for row in board["threads"]]))

        stable = fleet.by_key(first.key)
        lines = fleet.refresh()
        check("a beat over a fleet nobody wrote to reports no change",
              lines == [], str(lines))
        check("...and reuses each unchanged project rather than re-listing it",
              fleet.by_key(first.key) is stable
              and len(stable.threads) == 1,
              f"{fleet.by_key(first.key)} {stable.threads}")

        writer = facts["writers"][0]
        writer.execute(
            "insert into messages (thread_id, role, parts_json, attachments_json,"
            " metrics_json, ts) values (?,?,?,?,?,?)",
            (facts["threads"][first.label], "assistant",
             json.dumps([{"kind": "text", "text": "a reply that moves the beat"}]),
             "[]", "{}", WHEN + 1))
        writer.commit()
        lines = fleet.refresh()
        check("a commit in one source is seen on the next beat",
              any(facts["threads"][first.label] in line for line in lines),
              str(lines))
        check("...and that project is re-read, not reused",
              fleet.by_key(first.key) is not stable, str(fleet.by_key(first.key)))

        alternate = facts["directories"][last.label] / "alternate.db"
        alt = sqlite3.connect(alternate)
        try:
            alt.executescript(fixture.SCHEMA)
            alt.execute("insert into projects (id, path, created_at)"
                        " values ('alt','C:/work/Alt',?)", (WHEN,))
            alt.execute(
                "insert into threads (id, project_id, project_path, title, status,"
                " turn_state, model, created_at, updated_at, sidebar_archived_at)"
                " values ('alt-thread','alt','C:/work/Alt','Alt Thread','open',"
                " 'idle','claude',?,?,NULL)", (WHEN, WHEN))
            alt.execute(
                "insert into messages (thread_id, role, parts_json,"
                " attachments_json, metrics_json, ts) values (?,?,?,?,?,?)",
                ("alt-thread", "user",
                 json.dumps([{"kind": "text", "text": "alternate needle"}]),
                 "[]", "{}", WHEN))
            alt.commit()
        finally:
            alt.close()
        was = fleet.by_key(last.key)
        set_database(facts["directories"][last.label], "alternate.db")
        fleet.refresh()
        swapped = fleet.by_key(last.key)
        check("a project pointed at a different database is re-read, not reused",
              swapped is not was and messages_in(fleet, swapped) == 1
              and swapped.threads[0].id == "alt-thread", str(swapped))

        key = first.key
        held = [connection_for(fleet, first) for _ in range(8)]
        check("eight reads are pinned onto at most one connection per worker, "
              "not one per request",
              len({id(connection) for connection in held}) <= WORKERS,
              str(len({id(connection) for connection in held})))
        generation = fleet.generations.get(fleet.group_of(first), 0)

        set_database(facts["directories"][first.label], "missing-v3.db")
        fleet.refresh()
        check("a source that goes away moves its own group's generation",
              fleet.generations.get(key_group(key, fleet.ceiling), 0)
              == generation + 1,
              f"{fleet.generations.get(key_group(key, fleet.ceiling), 0)} "
              f"!= {generation + 1}")
        gone = fleet.by_key(key)
        check("the project whose source went away is the unreadable one",
              gone is not None and not gone.readable
              and gone.unreadable is not None, str(gone and gone.unreadable))
        peers = [project for project in fleet.projects if project.key != key]
        check("its group's other projects still read",
              all(project.readable for project in peers),
              "; ".join(f"{project.label}: {project.unreadable}"
                        for project in peers if not project.readable))
        peer = next(project for project in peers
                    if fleet.group_of(project) == fleet.group_of(first))
        check("a peer in the same group answers on the rebuilt connection",
              messages_in(fleet, peer) == 1)
        fresh = connection_for(fleet, peer)
        check("a read after the change rebuilt the group's connection",
              not any(fresh is connection for connection in held),
              str(fresh))

        set_database(facts["directories"][first.label], "desktop-v2.db")
        fleet.refresh()
        back = fleet.by_key(key)
        check("the next build recovers the project",
              back is not None and back.readable and len(back.threads) == 1,
              str(back and back.unreadable))

        print()
        if failures:
            print(f"FAIL: {len(failures)} pool check(s) failed")
            return 1
        print("ALL POOL TESTS PASS")
        return 0
    finally:
        fleet.close()
        fixture.close()
        for connection in facts["writers"]:
            try:
                connection.close()
            except sqlite3.Error:
                pass

STAMP_S = 0.4
BUDGET_S = 0.6

if __name__ == "__main__":
    testlog.start("test_pool")
    code = main()
    testlog.finish(code == 0)
    sys.exit(code)
