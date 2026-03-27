# Security Policy

## Reporting Security Issues

**DO NOT** report security vulnerabilities in public GitHub issues. Instead, please use GitHub's [Security Advisory](https://docs.github.com/en/code-security/security-advisories/sponsoring-improvements-to-repository-security-with-security-advisories) feature or contact the maintainer privately.

To report a security vulnerability:
1. Use GitHub's "Security" tab → "Report a vulnerability"
2. Or email the repository maintainer with a detailed description

## Security Best Practices

### 1. Secrets Management

**Never** commit secrets to this repository. This includes:
- Passwords, API keys, tokens
- Private keys (.key, .pem files)
- SSH keys
- Database credentials
- Vault tokens

Instead:
- Use [HashiCorp Vault](https://www.vaultproject.io/) for secret management
- Store secrets in Vault and reference them in playbooks via `vault_*` variables
- Use Vault AppRole for machine authentication
- Implement Vault agent for sidecar injection of credentials

### 2. Sensitive Data in Playbooks

When writing playbooks:
- Use `no_log: true` for tasks handling secrets:
  ```yaml
  - name: Configure sensitive data
    ansible.builtin.command: some_command
    no_log: true
  ```
- Never hardcode credentials in file templates or jinja2 variables
- Use lookup plugins with proper error handling:
  ```yaml
  password: "{{ lookup('community.hashi_vault.hashi_vault', 'secret=secret/data/my_secret field=password') }}"
  ```

### 3. Pre-commit Hooks

To prevent accidental secret commits, install a pre-commit hook:

```bash
pip install pre-commit detect-secrets
pre-commit install
```

Create a `.pre-commit-config.yaml` in your repo to scan for secrets before commits.

### 4. Git History

If you accidentally committed a secret:
1. **Immediately** rotate/revoke the secret
2. Use `git filter-branch` or `BFG Repo-Cleaner` to remove it from history
3. Force push and notify all contributors to rebase

Example with BFG:
```bash
bfg --replace-text secrets.txt [repo-url]
git reflog expire --expire=now --all && git gc --prune=now --aggressive
```

### 5. Repository Configuration

When using this repo in production:
- Use [Vault-backed Ansible dynamic inventory](https://docs.ansible.com/ansible/latest/plugins/lookup/hashi_vault.html)
- Enable SSH key authentication (disable password auth)
- Restrict host access via SSH keys in `group_vars` and `host_vars`
- Implement Vault policies to limit privilege elevation

### 6. Inventory Management

- **Never commit actual hostnames/IPs/credentials to version control**
- Use `inventory/production.ini.example` as a template
- Create `inventory/production.ini` from the example and exclude from git (already in `.gitignore`)
- Store actual inventory in Vault or secure external systems

### 7. SSL/TLS Certificates

- Don't commit private keys to git
- Public certificates (`.crt`, `.cer`) are safe to commit if they don't reveal infrastructure details
- CAs should be tracked separately via Vault or certificate management systems
- Use `inventory/certs/` only for public CAs (add to `.gitignore` for private keys)

### 8. Third-party Dependencies

- Regularly update Ansible collections and roles
- Review submodules for security updates:
  ```bash
  git submodule foreach git fetch origin
  git submodule foreach git log --oneline origin/main
  ```
- Run SAST/security scanning on playbooks (e.g., with Semgrep)

### 9. Container Images

If building or deploying containers:
- Scan images for vulnerabilities: `trivy image myimage:tag`
- Don't hardcode secrets in Dockerfiles
- Use multi-stage builds to reduce attack surface

### 10. Access Control

- Use SSH keys, not passwords (enforced by `enable_ssh_key_access.sh`)
- Implement passwordless sudo for service accounts (use `enable_passwordless_sudo_remote.sh`)
- Rotate keys regularly
- Implement least-privilege IAM/RBAC policies

## Security Scanning

This repository uses [Semgrep](https://semgrep.dev/) for Static Application Security Testing (SAST). The CI workflow (`ci: add Semgrep SAST GitHub workflow`) runs on all pull requests.

To run Semgrep locally:
```bash
pip install semgrep
semgrep --config p/ansible --config p/security-audit .
```

## Compliance Checklist

Before deploying to production:
- [ ] All secrets sourced from Vault, not git
- [ ] No hardcoded passwords, API keys, or tokens
- [ ] SSH key authentication configured
- [ ] Vault policies implement least privilege
- [ ] TLS/SSL certificates managed externally
- [ ] Inventory audit logging enabled
- [ ] Regular backup and disaster recovery tested
- [ ] SAST scanning passing (no high/critical issues)
- [ ] Dependency audit completed (no known vulns)

## Disclaimer

This repository is provided as-is. Users are responsible for:
- Securing their own infrastructure
- Validating playbooks before production use
- Implementing appropriate access controls
- Following their organization's security policies

For security concerns about the Ansible automation framework itself, see:
- [Ansible Security Best Practices](https://docs.ansible.com/ansible/latest/user_guide/vault.html)
- [OWASP Infrastructure Security](https://owasp.org/)
- [CIS Benchmarks](https://www.cisecurity.org/cis-benchmarks/)
