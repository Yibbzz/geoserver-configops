"""One apply at a time: a lock file in GeoServer's data directory.

Two applies at once work from two plans of the same GeoServer and undo each other's work
(rest.properties and the security files are read and written whole). Every apply talks to
the same GeoServer, from a laptop, a pipeline or the cluster, so the lock is kept there:
the file `configops/apply.lock`, read and written through the REST API's /resource.

It holds who took it, a token and how long it is good for. GeoServer has no "write only if
it isn't there", so taking the lock is write, then read back twice, a moment apart: of two applies
that write in the same instant, the one whose token is still there has it. For both to believe
they hold it, one must read "free", then take longer than that to write; it is not a guarantee, it
is what makes a pipeline and a person, or two Jobs, not run through each other.

A lock whose apply died is taken over once it has gone unrenewed for its time to live. Its
age is GeoServer's own clock (the answer's Date and Last-Modified), not this machine's. A
running apply renews the lock before each change.
"""

from __future__ import annotations

import getpass
import json
import os
import socket
import time
import uuid
from email.utils import parsedate_to_datetime

from .geoserver import GeoServerError

PATH = "resource/configops/apply.lock"
TTL = 900  # seconds a lock is good for without being renewed
SETTLE = 0.4  # seconds between writing the lock and each of the two times it is read back


class LockHeld(Exception):
    """Another apply holds the lock."""


def holder() -> str:
    try:
        user = getpass.getuser()
    except (KeyError, OSError):  # no name for this uid, as in a container
        user = f"uid {os.getuid()}" if hasattr(os, "getuid") else "?"
    return f"{user}@{socket.gethostname()} (pid {os.getpid()})"


class Lock:
    def __init__(self, gs, ttl: float = TTL, settle: float = SETTLE):
        self.gs, self.ttl, self.settle = gs, ttl, settle
        self.token, self.written = uuid.uuid4().hex, 0.0

    # --- the file

    def read(self) -> tuple[dict, float] | None:
        """(the lock, its age in seconds by GeoServer's clock), or None when there is none. A file that isn't a
        lock of ours (emptied, cut short) reads as a lock without a token, as old as the file is."""
        r = self.gs.request("GET", PATH, ok=(200, 404))
        if r.status_code == 404:
            return None
        try:
            data = json.loads(r.text)
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        headers = getattr(r, "headers", None) or {}
        try:
            age = (parsedate_to_datetime(headers["Date"]) - parsedate_to_datetime(headers["Last-Modified"])).total_seconds()
        except (KeyError, TypeError, ValueError):  # no such headers: by the time the lock itself gives, and this clock
            age = time.time() - float(data.get("written") or 0)
        return data, max(0.0, age)

    def write(self) -> None:
        body = json.dumps({"token": self.token, "holder": holder(), "ttl": self.ttl, "written": time.time()})
        self.gs.request("PUT", PATH, body, content_type="text/plain")
        self.written = time.monotonic()

    # --- taking, keeping, giving back

    def free(self, found: tuple[dict, float] | None) -> bool:
        """Whether the lock can be taken: there is none, it is ours, or it went unrenewed for its time to live."""
        if found is None:
            return True
        data, age = found
        try:
            ttl = float(data.get("ttl") or self.ttl)
        except (TypeError, ValueError):
            ttl = self.ttl
        return data.get("token") == self.token or age > ttl

    def acquire(self, wait: float = 0, steal: bool = False) -> None:
        """Take the lock, or raise LockHeld saying who has it. `wait`: seconds to keep trying. `steal`: take it
        whoever has it (after an apply that was killed, before its lock has run out)."""
        deadline = time.monotonic() + wait
        while True:
            found = self.read()
            if steal or self.free(found):
                self.write()
                for _ in range(2):  # read back twice, a moment apart: a write that crossed ours shows in one of them
                    time.sleep(self.settle)
                    after = self.read()
                    if after is None or after[0].get("token") != self.token:
                        break
                else:
                    return
                found = after  # someone wrote in the same moment, and theirs stayed
            if time.monotonic() >= deadline:
                data, age = found if found is not None else ({}, 0.0)
                raise LockHeld(f"another apply holds the lock: {data.get('holder') or 'unknown'}, {age:.0f} seconds ago "
                               f"(it runs out after {float(data.get('ttl') or self.ttl):.0f} unrenewed). Nothing was applied. "
                               "Wait for it (--lock-wait SECONDS), or, if that apply is dead, take the lock over (--break-lock).")
            time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))

    def keep(self) -> None:
        """Before each change: renew the lock when a third of its time has gone, and stop if it is no longer ours
        (it ran out during a very long change and another apply took it)."""
        if time.monotonic() - self.written < self.ttl / 3:
            return
        found = self.read()
        if found is None or found[0].get("token") != self.token:
            who = (found[0].get("holder") if found else None) or "nobody"
            raise LockHeld(f"this apply's lock was lost (now held by {who}): it went unrenewed for more than {self.ttl:.0f} "
                           "seconds during one change. Nothing more was applied; run plan to see where things stand.")
        self.write()

    def release(self) -> None:
        """Give the lock back, if it is still ours. Never raises: the apply's own result is what matters by now."""
        try:
            found = self.read()
            if found is not None and found[0].get("token") == self.token:
                self.gs.request("DELETE", PATH, ok=(200, 404))
        except (GeoServerError, OSError):
            pass
