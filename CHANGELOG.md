# Changelog

## 0.2.0

A release about running `apply` safely in production.

**Upgrading from 0.1.0**

- Each store with a login shows one `credentials: (not recorded yet)` change on its first `plan`. One `apply` records it.
- `plan` and `apply` exit 1, not 2, when the files are invalid or a Secret is missing. 2 now only means "there are changes".
- `apply --prune` stops before deleting more than 10 things; pass `--max-deletes N` to allow more.
- `apply` takes a lock in GeoServer's data directory (`configops/apply.lock`). A second apply exits 1 unless it is given `--lock-wait`.
- `apply` no longer recalculates a layer's bounds or reseeds its tiles on unrelated changes. Run `configops refresh` after loading new data.
- The example drift CronJob runs `plan --fail-on-unmanaged`.
- `docs/kubernetes/Dockerfile` builds from a configops version (`CONFIGOPS_VERSION`), not `latest`.

**Fixed**

- A layer with `enabled: false` was switched on by every apply.
- Access rules were applied after the layers they protect. They now come first, and are removed last.
- A layer with `attributes` could be served with every column while its list was being set.
- Materialized views, and tables the database login can't see, were never published.
- A layer whose table was briefly missing was taken out of its groups.
- Moving a layer to another store failed when a group held it.
- A store password changed in the Secret was never applied.
- `plan` printed WMS/WMTS header values, authentication keys, auth provider passwords, S3 access key IDs, and keys in store URLs.
- Raw tracebacks on database errors, unreadable files and non-JSON answers.

**Added**

- `plan --digest` and `apply --expect`, to apply exactly the plan that was reviewed.
- `plan --fail-on-unmanaged`, so a user, role or rule added in the web UI fails a drift check.
- `{ secret, key }` for passwords in auth provider and other security object configs.
- `validate` catches duplicate YAML keys, styles GeoServer couldn't parse, and a Store named after a schema configops already uses.
- `CONFIGOPS_TIMEOUT`, `CONFIGOPS_GEOSERVER_CLOUD`, `CONFIGOPS_DEBUG`, and `sslmode` in the database Secret.
- The image installs pinned, hash-checked dependencies; GitHub Actions are pinned to commit SHAs.

**Known limitations**

- `apply` is not a transaction: if it stops part-way, what it changed stays, and running it again finishes.
- A new layer is enabled for one request before configops switches it off; an `AccessRule` covers that moment.
- A store password changed in the web UI is not detected.
- The apply lock can, rarely, be taken by two applies started within the same half second.
