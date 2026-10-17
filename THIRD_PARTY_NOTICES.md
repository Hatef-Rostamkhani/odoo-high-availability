# Third-party components

The root MIT license covers original code and documentation in this repository.
It does not relicense upstream charts, schemas, images, bundled dependencies or
trademarks. Images and CLI binaries are downloaded from their suppliers and are
not included in the source distribution. Custom addons are excluded by default.

| Component | Pinned source | License/terms reference |
|---|---|---|
| Cilium chart | `vendor/charts.lock.json` | [Apache-2.0](third-party/licenses/cilium-Apache-2.0.txt) |
| Argo Helm charts | `vendor/charts.lock.json` | [Apache-2.0](third-party/licenses/argo-helm.txt) |
| Prometheus community charts | `vendor/charts.lock.json` | [Apache-2.0](third-party/licenses/prometheus-charts.txt); bundled subcharts retain their own notices |
| External Secrets chart | `scripts/openbao.py`, vendored chart | [Apache-2.0](third-party/licenses/external-secrets.txt) |
| OpenBao chart and server | `scripts/openbao.py`, image lock | [Chart MPL-2.0](third-party/licenses/openbao-helm.txt), [server MPL-2.0](third-party/licenses/openbao.txt) |
| Kubernetes schemas/manifests | Version and vendor locks | [Apache-2.0](third-party/licenses/kubernetes.txt) |
| Crunchy packaged PGO/chart/images | Database HA lock files | [Supplier notice](third-party/licenses/crunchy-postgres-terms.txt), [Developer Program terms](https://www.crunchydata.com/developers/terms-of-use) |
| Odoo Community image | `versions.lock.json` | [Odoo 19 licensing](https://github.com/odoo/odoo/blob/19.0/LICENSE); individual addons may use other licenses |
| PostgreSQL image | `versions.lock.json` | [PostgreSQL license](https://www.postgresql.org/about/licence/); image packages retain their own terms |

The Crunchy packaged chart is intentionally ignored by Git and excluded from
public source archives. `scripts/vendor-tools.py` fetches its locked OCI digest
directly from the supplier for schema validation and optional HA use. Its
packaged software has separate use and redistribution terms; the project's MIT
license grants no rights over that distribution. Review the supplier's current
terms before using the HA profile. The basic profile uses the official PostgreSQL
image and does not install Crunchy components.

Upstream notice texts are preserved in `third-party/licenses`; consult each
artifact's bundled notices for its complete dependency set. Lock files preserve
artifact origins and hashes. This inventory documents project dependencies and
does not replace their individual license terms.
