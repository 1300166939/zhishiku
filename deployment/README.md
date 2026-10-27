# Deployment configuration

Place code in `/opt/ai-knowledge-base` and private configuration in `/etc/ai-knowledge-base`. These paths are generic installation conventions, not production host identities.

Provide three data roots, a trusted collector principal and a validated credential policy. Bearer tokens are generated privately through the management tools; no token values belong in source control. Do not initialize the gateway with legacy all-domain credentials by accident. An empty policy denies all requests until credentials are enrolled by the administrator.

`gateway.env.example`, `model.env.example` and `qdrant.env.example` contain configuration names only. Do not start a real service with blank credential values. Serve Qdrant on loopback with an API key and provision the collections through the core adapter. Use your own TLS certificate and reverse proxy to port 8088.

`ai-kb-connector-worker.service` is an optional manual Linux oneshot unit (CPU 50%, memory 1 GiB, tasks 32). It runs one queued item; it installs no timer and provides no recurring collection. Provision the `aikb` service account, directory permissions and gateway/operator services separately.

Model calls use the official Ark API; rerank remains optional. The privileged Unix-socket operator must not be exposed directly to the Internet. Agent, owner and source authorization still require administrator-managed policy.

Run the offline tests before deployment. Full/offsite backups, valid business versions, independent evaluation and continuous-load sizing require deployment-specific acceptance.
