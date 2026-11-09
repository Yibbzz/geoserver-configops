# Notice

The Helm chart in this repository started from Camptocamp's
[helm-geoserver-cloud](https://github.com/camptocamp/helm-geoserver-cloud)
(BSD 2-Clause, Copyright (c) 2011-2024 Camptocamp SA).

The vendored chart under `charts/` keeps its own licence:

- `charts/application`: [camptocamp/helm-application](https://github.com/camptocamp/helm-application) (see `charts/application/LICENSE`)

PostGIS and RabbitMQ run from their official images, [postgis/postgis](https://hub.docker.com/r/postgis/postgis) and [rabbitmq](https://hub.docker.com/_/rabbitmq), with this chart's own templates.

GeoServer Cloud itself is developed at [geoserver/geoserver-cloud](https://github.com/geoserver/geoserver-cloud).

Data loaded by the ingest job keeps its own licence. Ordnance Survey OpenData is
released under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)
and must be attributed: "Contains OS data © Crown copyright and database right <year>".
