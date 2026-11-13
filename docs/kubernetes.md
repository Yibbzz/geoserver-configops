# configops in Kubernetes

configops runs beside a GeoServer or GeoServer Cloud you already have, as a Job and a CronJob.
[`kubernetes/configops.yaml`](kubernetes/configops.yaml) has:

- `configops-apply`: a Job that makes GeoServer match the files;
- `configops-drift`: a CronJob that runs `configops plan` hourly. Its Job fails when GeoServer no longer matches the files (say a layer changed in the web UI), so drift shows in `kubectl get jobs` and in any alerts on failed Jobs.

Loading data on a schedule is geodata-sync's: its own `docs/kubernetes.md` has a CronJob that loads, then runs `configops refresh` on what it loaded.

## 1. An image with your project in it

The project travels in an image, so every Job runs the same files, and a new version is a new tag.
[`kubernetes/Dockerfile`](kubernetes/Dockerfile) copies a project folder into the configops image:

```bash
docker build -f docs/kubernetes/Dockerfile --build-arg PROJECT=my-project -t registry.example.org/my-project-configops:1 .
docker push registry.example.org/my-project-configops:1
```

Leave `secrets.yaml` out of the image (`.dockerignore`): logins come from Secrets.

## 2. Secrets

Each Secret the project names (`credentials: { secret: ... }`, `connection: { secret: ... }`) is mounted as a folder under `/etc/configops/secrets/<secret>`, one file per key. For the quick start's names:

```bash
kubectl create secret generic geoserver-admin --from-literal=username=admin --from-literal=password=...
kubectl create secret generic database --from-literal=host=... --from-literal=port=5432 \
  --from-literal=database=... --from-literal=username=... --from-literal=password=...
```

The hosts in them are as configops reaches them from its own namespace: `<service>.<namespace>` when GeoServer or the database is in another one.

Add a volume and a mount to each Job for every other Secret the project names (users' passwords, a blob store's keys, an API key). A missing optional one can be marked `optional: true`.

## 3. Apply

Set the image in `configops.yaml`, then:

```bash
kubectl apply -f configops.yaml
kubectl logs -f job/configops-apply
```

Inside the cluster the project's `geoserver.url` is GeoServer's Service, for example `http://geoserver-gsc-gateway:8080/geoserver-cloud`. To apply a new version, change the image tag, delete the finished Job (`kubectl delete job configops-apply`) and apply the file again; a CD tool (Argo CD, Flux) can do this on every commit.

## Drift

```bash
kubectl get jobs -l batch.kubernetes.io/cronjob-name=configops-drift   # Failed = drift
kubectl logs job/<the failed one>                                      # what differs
```

`plan` exits 3 when only `!` lines are left (layers waiting for their tables, settings GeoServer Cloud can't take): the drift Job treats that as no drift.

## Files in GeoServer's /data

An `appschema` store makes a folder beside its mapping, where GeoServer keeps the schemas it downloads. For that, mount the same volume GeoServer's services use at `/data` in the Jobs too (a `persistentVolumeClaim` volume; `ReadWriteMany` if the pods can run on different nodes).
