"""TEMPORARY (not for merge): what happens to the keystore when the master password changes on a fresh,
multi-service GeoServer Cloud.   python masterpw-diagnose.py <gateway base url> <namespace>"""
import hashlib
import secrets
import subprocess
import sys
import tempfile
import time

import requests

BASE, NS = sys.argv[1] + "/rest", sys.argv[2]
s = requests.Session()
s.auth = ("admin", "geoserver")
PWS: dict[str, str] = {}  # label -> master password, so output never shows one
START = time.time()
broken = False


def say(*a):
    print(f"[{time.time() - START:6.1f}s]", *a, flush=True)


def master():
    r = s.get(f"{BASE}/security/masterpw.json")
    if r.status_code != 200:
        return f"HTTP {r.status_code} {r.text[:200]}"
    pw = r.json()["oldMasterPassword"]
    return next((k for k, v in PWS.items() if v == pw), "unknown")


def keystore(name):
    r = s.get(f"{BASE}/resource/security/{name}")
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    out = f"{len(r.content)} bytes sha {hashlib.sha256(r.content).hexdigest()[:10]}"
    with tempfile.NamedTemporaryFile(suffix=".jceks") as f:
        f.write(r.content)
        f.flush()
        for label, pw in PWS.items():
            k = subprocess.run(["keytool", "-list", "-storetype", "JCEKS", "-keystore", f.name, "-storepass", pw],
                               capture_output=True, text=True)
            if k.returncode == 0:
                aliases = sorted(line.split(",")[0] for line in k.stdout.splitlines() if "Entry" in line)
                out += f"; opens with {label}: {aliases}"
    return out if "opens" in out else out + "; opens with NO known password"


def store(name):
    r = s.get(f"{BASE}/workspaces/mpw/datastores/{name}.json")
    if r.status_code == 200:
        return "200 ok"
    return f"HTTP {r.status_code} {r.text[:160]!r}"


def make_store(name):
    params = {"host": "nowhere", "port": "5432", "database": "x", "user": "u", "passwd": "store-secret", "dbtype": "postgis"}
    body = {"dataStore": {"name": name, "connectionParameters": {"entry": [{"@key": k, "$": v} for k, v in params.items()]}}}
    r = s.post(f"{BASE}/workspaces/mpw/datastores", json=body)
    say(f"POST store {name}: {r.status_code} {r.text[:160]!r}")


STORES: list[str] = []


def state(label):
    global broken
    say(f"== {label}")
    say("   master password is:", master())
    say("   geoserver.jceks:", keystore("geoserver.jceks"))
    say("   geoserver.jceks.new:", keystore("geoserver.jceks.new"))
    for name in STORES:
        got = store(name)
        broken = broken or got != "200 ok"
        say(f"   GET store {name}:", got)


def change(label):
    PWS[label] = f"diag-{secrets.token_hex(6)}"
    cur = s.get(f"{BASE}/security/masterpw.json").json()["oldMasterPassword"]
    r = s.put(f"{BASE}/security/masterpw.json", json={"oldMasterPassword": cur, "newMasterPassword": PWS[label]})
    say(f"PUT masterpw -> {label}: {r.status_code} {r.text[:200]!r}")


def logs(since):
    for pod in subprocess.run(["kubectl", "-n", NS, "get", "pods", "-o", "name"], capture_output=True, text=True).stdout.split():
        if "gsc-" not in pod or "gateway" in pod:
            continue
        out = subprocess.run(["kubectl", "-n", NS, "logs", pod, f"--since={since}s"], capture_output=True, text=True).stdout
        keep = [line[:330] for line in out.splitlines()
                if any(w in line for w in ("jceks", "eystore", "KeyStore", "aster password", "MasterPassword", "ERROR", "SEVERE",
                                           "Exception", "Config already being reloaded"))]
        print(f"--- {pod}: {len(keep)} lines")
        print("\n".join(keep[:60]), flush=True)


def restart(*deploys):
    say("restarting", deploys)
    for d in deploys:
        subprocess.run(["kubectl", "-n", NS, "rollout", "restart", f"deploy/{d}"], check=False)
    for d in deploys:
        subprocess.run(["kubectl", "-n", NS, "rollout", "status", f"deploy/{d}", "--timeout=8m"], check=False)
    for _ in range(60):
        try:
            if s.get(f"{BASE}/about/version.json", timeout=10).status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(5)


def recover():
    logs(int(time.time() - START) + 5)
    restart("geoserver-gsc-rest")
    state("after restarting REST")
    make_store(f"after_restart_{len(STORES)}")
    STORES.append(f"after_restart_{len(STORES)}")
    state("a store saved after the REST restart")


PWS["fresh"] = s.get(f"{BASE}/security/masterpw.json").json()["oldMasterPassword"]
say("POST workspace:", s.post(f"{BASE}/workspaces", json={"workspace": {"name": "mpw"}}).status_code)
state("fresh install")
make_store("before")
STORES.append("before")
state("a store with a password saved")

change("pw1")
state("straight after ONE change")
time.sleep(10)
state("10 s later")
time.sleep(40)
state("50 s later")
make_store("after1")
STORES.append("after1")
state("a store saved after the change")

if not broken:
    say("#### one change did no harm; now three in a row, as the e2e test does")
    for i in (2, 3, 4):
        change(f"pw{i}")
        say("   master password is:", master())
    state("straight after three changes")
    time.sleep(50)
    state("50 s later")
    make_store("after3")
    STORES.append("after3")
    state("a store saved after the three changes")

say("#### BROKEN" if broken else "#### nothing broke")
recover()
logs(120)
