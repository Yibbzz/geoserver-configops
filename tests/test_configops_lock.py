"""The apply lock (configops/lock.py) against a GeoServer /resource that remembers files and when they were written."""

import json
import types
from email.utils import formatdate

import pytest

from configops import lock as lock_module
from configops.geoserver import GeoServerError
from configops.lock import PATH, Lock, LockHeld


class Resources:
    """GeoServer's /resource: one file store and GeoServer's own clock (`now`, in seconds)."""

    def __init__(self):
        self.files, self.now, self.calls, self.after_put = {}, 1_000_000.0, [], None

    def request(self, method, path, body=None, content_type=None, ok=(200, 201)):
        self.calls.append((method, path))
        if method == "GET":
            if path not in self.files:
                return types.SimpleNamespace(status_code=404, text="", headers={})
            text, written = self.files[path]
            return types.SimpleNamespace(status_code=200, text=text,
                                         headers={"Date": formatdate(self.now, usegmt=True), "Last-Modified": formatdate(written, usegmt=True)})
        if method == "PUT":
            self.files[path] = (body, self.now)
            if self.after_put:
                hook, self.after_put = self.after_put, None
                hook()
        elif method == "DELETE":
            self.files.pop(path, None)
        return types.SimpleNamespace(status_code=200, text="", headers={})


@pytest.fixture
def gs(monkeypatch):
    monkeypatch.setattr(lock_module.time, "sleep", lambda seconds: None)
    return Resources()


def test_the_lock_is_taken_and_given_back(gs):
    mine = Lock(gs)
    mine.acquire()
    held = json.loads(gs.files[PATH][0])
    assert held["token"] == mine.token and "pid" in held["holder"] and held["ttl"] == 900
    mine.release()
    assert PATH not in gs.files


def test_a_second_apply_is_told_who_holds_it(gs):
    Lock(gs).acquire()
    gs.now += 60
    with pytest.raises(LockHeld, match=r"another apply holds the lock: .+pid \d+\), 60 seconds ago .*--lock-wait.*--break-lock"):
        Lock(gs).acquire()


def test_a_dead_applys_lock_runs_out_by_geoservers_clock(gs):
    Lock(gs).acquire()
    gs.now += 899
    with pytest.raises(LockHeld):
        Lock(gs).acquire()
    gs.now += 2
    second = Lock(gs)
    second.acquire()
    assert json.loads(gs.files[PATH][0])["token"] == second.token


def test_break_lock_takes_it_over_at_once(gs):
    Lock(gs).acquire()
    second = Lock(gs)
    second.acquire(steal=True)
    assert json.loads(gs.files[PATH][0])["token"] == second.token


def test_of_two_that_write_in_the_same_moment_one_has_it(gs):
    first, second = Lock(gs), Lock(gs)
    gs.after_put = second.write  # the other apply's write lands right after ours, before we read back
    with pytest.raises(LockHeld):
        first.acquire()
    assert json.loads(gs.files[PATH][0])["token"] == second.token


def test_waiting_for_the_lock(gs, monkeypatch):
    first, second = Lock(gs), Lock(gs)
    first.acquire()
    sleeps = []

    def sleep(seconds):  # the first apply finishes while the second waits
        sleeps.append(seconds)
        if len(sleeps) == 2:
            first.release()

    monkeypatch.setattr(lock_module.time, "sleep", sleep)
    second.acquire(wait=30)
    assert json.loads(gs.files[PATH][0])["token"] == second.token


def test_a_running_apply_renews_its_lock_and_stops_when_it_lost_it(gs, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(lock_module.time, "monotonic", lambda: clock[0])
    mine = Lock(gs)
    mine.acquire()
    gs.calls.clear()
    mine.keep()
    assert gs.calls == []  # just taken: nothing to do
    clock[0] += 400
    gs.now += 400
    mine.keep()
    assert gs.files[PATH][1] == gs.now  # written again: good for another 15 minutes
    clock[0] += 400
    other = Lock(gs)
    other.write()  # it ran out during one very long change, and another apply has it
    with pytest.raises(LockHeld, match="this apply's lock was lost"):
        mine.keep()


def test_release_leaves_another_applys_lock_alone_and_never_raises(gs):
    mine, other = Lock(gs), Lock(gs)
    mine.acquire()
    other.write()
    mine.release()
    assert json.loads(gs.files[PATH][0])["token"] == other.token

    def down(*args, **kwargs):
        raise GeoServerError("GET", PATH, 0, "can't reach GeoServer")

    gs.request = down
    mine.release()


def test_a_file_that_isnt_a_lock_is_taken_over_only_when_old(gs):
    gs.files[PATH] = ("", gs.now)
    with pytest.raises(LockHeld, match="unknown"):
        Lock(gs).acquire()
    gs.now += 901
    Lock(gs).acquire()
