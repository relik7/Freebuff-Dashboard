from __future__ import annotations

import threading

WATCH_TAG = "[watch]"

WATCH_ALIVE_BEATS = 12

class Watcher:
    def __init__(self, config: dict, fleet, log) -> None:
        self.config = config
        self.fleet = fleet
        self.log = log
        self.seconds = float((config.get("data") or {}).get("watch_seconds") or 0)
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.condition = threading.Condition()
        self.version = 0
        self.lines: list[str] = []

    def refresh(self) -> list[str]:
        lines = self.fleet.refresh()
        if not lines:
            return lines
        self.log(f"{WATCH_TAG} {len(lines)} change(s)")
        for line in lines:
            self.log(f"{WATCH_TAG} {line}")
        with self.condition:
            self.version += 1
            self.lines = lines
            self.condition.notify_all()
        return lines

    def watched(self, seen: int, timeout: float) -> tuple[int, list[str]]:
        with self.condition:
            self.condition.wait_for(lambda: self.version != seen, timeout)
            return self.version, list(self.lines)

    def listing(self) -> str:
        projects = self.fleet.projects
        return (f"{len(projects)} project(s), "
                f"{sum(len(project.threads) for project in projects)}"
                f" listed thread(s)")

    def run(self) -> None:
        self.log(f"{WATCH_TAG} watching {self.listing()} every {self.seconds:g} s;"
                 f" a change is logged and pushed to every open page")
        beats = 0
        while not self.stopped.wait(self.seconds):
            beats += 1
            if not self.refresh() and beats % WATCH_ALIVE_BEATS == 0:
                self.log(f"{WATCH_TAG} beat {beats}: nothing changed"
                         f" ({self.listing()})")

    def start(self) -> None:
        if self.seconds <= 0:
            return
        self.thread = threading.Thread(target=self.run, name="fb-dashboard-watch",
                                       daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stopped.set()
        if self.thread is not None:
            self.thread.join(timeout=max(2.0, self.seconds + 1))
            self.thread = None
