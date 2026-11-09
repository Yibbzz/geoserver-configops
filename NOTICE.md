# Notice

The Helm chart in this repository started from Camptocamp's
[helm-geoserver-cloud](https://github.com/camptocamp/helm-geoserver-cloud)
(BSD 2-Clause, Copyright (c) 2011-2024 Camptocamp SA).

Vendored dependency charts under `charts/` keep their own licences:

- `charts/application`: [camptocamp/helm-application](https://github.com/camptocamp/helm-application) (see `charts/application/LICENSE`)
- `charts/postgresql`, `charts/rabbitmq`: Bitnami charts (Apache 2.0), via [georchestra/bitnami-helm-charts](https://github.com/georchestra/bitnami-helm-charts)

GeoServer Cloud itself is developed at [geoserver/geoserver-cloud](https://github.com/geoserver/geoserver-cloud).

Data loaded by the ingest job keeps its own licence. Ordnance Survey OpenData is
released under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)
and must be attributed: "Contains OS data © Crown copyright and database right <year>".
