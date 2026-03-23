# homelab_ansible

Host and node configuration automation for the homelab infrastructure outside Kubernetes manifests.

## Related repositories

This repository is one part of a shared homelab stack:

- [homelab_bootstrap](https://github.com/richrobertson/homelab_bootstrap) - first-stage cluster bootstrap/orchestration before Flux management.
- [homelab_ansible](https://github.com/richrobertson/homelab_ansible) - host and node configuration automation outside Kubernetes manifests.
- [homelab_flux](https://github.com/richrobertson/homelab_flux) - in-cluster GitOps state (apps, controllers, configs, and gateway resources).
