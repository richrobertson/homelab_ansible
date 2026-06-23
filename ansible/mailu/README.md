# Mailu playbooks

## SMTP abuse containment

`ansible/mailu/smtp_abuse_containment.yml` captures evidence, optionally holds or deletes the Postfix queue, and stops the Mailu `smtp` container to halt outbound abuse while preserving inbound-capable services.

Safe default run:

```bash
ansible-playbook -i inventory/environments/production.ini ansible/mailu/smtp_abuse_containment.yml
```

Useful overrides:

```bash
ansible-playbook -i inventory/environments/production.ini ansible/mailu/smtp_abuse_containment.yml \
  -e mailu_log_since=2026-06-23T15:45:00Z \
  -e mailu_stop_smtp=true \
  -e mailu_hold_queue=true
```

Only delete the queue after preserving evidence and confirming it is abusive:

```bash
ansible-playbook -i inventory/environments/production.ini ansible/mailu/smtp_abuse_containment.yml \
  -e mailu_delete_queue=true
```
