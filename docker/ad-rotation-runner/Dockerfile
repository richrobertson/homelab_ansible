# Runner image for UNATTENDED AD service-account rotation.
#
# Runs scripts/rotate_due_service_accounts.py, which calls
# ansible/domain/rotate_service_account_passwords.yml once per due account.
# Built by .github/workflows/build-ad-rotation-runner.yml from a clean checkout,
# so only committed files can ever reach it; on top of that it copies ONLY the
# paths the rotation needs - never the whole repo.
#
# Every download is pinned by version AND verified by SHA-256. Bump the pins
# together with the checksums (sources noted next to each).
FROM python:3.12-slim-bookworm

ARG VAULT_VERSION=1.20.4
# https://releases.hashicorp.com/vault/${VAULT_VERSION}/vault_${VAULT_VERSION}_SHA256SUMS
ARG VAULT_SHA256=fc5fb5d01d192f1216b139fb5c6af17e3af742aaeffc289fd861920ec55f2c9c
ARG KUBECTL_VERSION=v1.34.1
# https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256
ARG KUBECTL_SHA256=7721f265e18709862655affba5343e85e1980639395d5754473dafaadcaa69e3

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections \
    ANSIBLE_HOST_KEY_CHECKING=false \
    ANSIBLE_RETRY_FILES_ENABLED=false

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSLo /tmp/vault.zip "https://releases.hashicorp.com/vault/${VAULT_VERSION}/vault_${VAULT_VERSION}_linux_amd64.zip" \
 && echo "${VAULT_SHA256}  /tmp/vault.zip" | sha256sum -c - \
 && unzip -q /tmp/vault.zip -d /usr/local/bin vault \
 && rm /tmp/vault.zip \
 && curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
 && echo "${KUBECTL_SHA256}  /usr/local/bin/kubectl" | sha256sum -c - \
 && chmod 0755 /usr/local/bin/kubectl /usr/local/bin/vault \
 && vault version && kubectl version --client

COPY docker/ad-rotation-runner/requirements.txt docker/ad-rotation-runner/collections.yml /tmp/
RUN pip install -r /tmp/requirements.txt \
 && ansible-galaxy collection install -r /tmp/collections.yml -p "${ANSIBLE_COLLECTIONS_PATH}" \
 && rm -rf /tmp/requirements.txt /tmp/collections.yml /tmp/ansible-local /root/.ansible /home/runner

RUN useradd --uid 10001 --create-home --home-dir /home/runner --shell /usr/sbin/nologin runner
WORKDIR /opt/homelab_ansible

# Only what the rotation needs. ansible.cfg comes along for its defaults, but the
# wrapper always passes -i inventory/runner/rotation.ini explicitly.
COPY --chown=root:root ansible.cfg ./ansible.cfg
COPY --chown=root:root ansible/domain/rotate_service_account_passwords.yml ./ansible/domain/
COPY --chown=root:root ansible/domain/vars/ ./ansible/domain/vars/
COPY --chown=root:root inventory/runner/ ./inventory/runner/
COPY --chown=root:root scripts/rotate_due_service_accounts.py scripts/rotate_due_k8s_secrets.py scripts/rotation_aws.py scripts/rotation_b2.py scripts/rotation_nextcloud_admin.py scripts/rotation_keycloak_oidc.py scripts/rotation_syncthing.py ./scripts/
# k8s_secret-class rotation map (scripts/rotate_due_k8s_secrets.py).
COPY --chown=root:root ansible/k8s/vars/ ./ansible/k8s/vars/

USER 10001
# HOME and the controller temp dir are set only AFTER switching user. Set any
# earlier, the root-run ansible-galaxy step pre-creates them root-owned (it did,
# twice: /tmp/ansible-local, then /home/runner/.ansible) and every ansible-*
# command as uid 10001 dies with EACCES on DEFAULT_LOCAL_TMP. In k8s this lives on the emptyDir mounted at /home/runner.
ENV HOME=/home/runner \
    ANSIBLE_LOCAL_TEMP=/home/runner/.ansible/tmp
ENTRYPOINT ["python3", "/opt/homelab_ansible/scripts/rotate_due_service_accounts.py"]
