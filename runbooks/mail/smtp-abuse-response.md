# SMTP abuse response runbook

Use this runbook when abuse reports, DSNs, or provider complaints indicate that `mail.myrobertson.net` or a Mailu account may be sending unsolicited mail.

## Triage the report first

1. Preserve the abuse report and download/open the embedded original message. The visible `email-abuse.amazon...` IP is usually the receiver/reporting system, not proof that the listed IP authenticated to your SMTP server.
2. In the embedded message, look for these headers and timestamps:
   - `Authentication-Results`
   - `Received`
   - `X-Authenticated-User`, `X-Mailu-User`, `X-Sasl-Username`, or similar authenticated-user headers
   - envelope sender, `From`, `Message-ID`, and first handoff timestamp
3. Treat any authenticated sender in the embedded message as compromised until logs prove otherwise.

## Immediate containment

If Ansible can reach the Mailu host, use the automated containment playbook first; it captures evidence, holds the queue, and stops the SMTP container with safe defaults:

```bash
ansible-playbook -i inventory/environments/production.ini ansible/mailu/smtp_abuse_containment.yml \
  -e mailu_log_since=2026-06-23T15:45:00Z
```

Run the remaining commands manually from the Mailu host if Ansible is unavailable. Adjust the compose path if the stack lives elsewhere.

```bash
cd /opt/mailu
sudo docker compose ps
```

Stop outbound SMTP while preserving inbound delivery if abuse is active and you cannot identify the account within a few minutes:

```bash
cd /opt/mailu
sudo docker compose stop smtp
```

If the compromised account is known, disable it before restarting SMTP. Prefer the Mailu admin UI when available. If shell access is the only option, inspect the available admin CLI first because Mailu command names can vary by version:

```bash
cd /opt/mailu
sudo docker compose exec admin flask mailu --help
sudo docker compose exec admin flask mailu user --help
```

Then disable the account or change its password using the supported command shown by the help output. Rotate the password for any account used by devices or apps, especially notification-only accounts.

## Identify the sending account

Search recent SMTP/auth logs around the complaint timestamp. The example below starts at `2026-06-23 15:45:00 UTC`; replace it with the timestamp from the complaint.

```bash
cd /opt/mailu
sudo docker compose logs --since '2026-06-23T15:45:00Z' smtp front admin \
  | egrep -i 'sasl|auth|login|client=|from=|sender|reject|queue|status=sent|54\.240\.27\.158|noreply@myrobertson\.net'
```

Correlate the queue IDs and authenticated username:

```bash
cd /opt/mailu
sudo docker compose logs --since '2026-06-23T15:45:00Z' smtp \
  | egrep -i 'sasl_username|client=|from=|to=|message-id|status=sent|removed'
```

Check the live queue before flushing or deleting anything:

```bash
cd /opt/mailu
sudo docker compose exec smtp postqueue -p
```

If the queue contains abusive outbound mail, hold or delete only after capturing enough evidence for incident notes:

```bash
cd /opt/mailu
sudo docker compose exec smtp postqueue -p > /tmp/mail-queue-before-cleanup.txt
sudo docker compose exec smtp postsuper -h ALL
```

Delete messages only when you are sure they are abusive:

```bash
cd /opt/mailu
sudo docker compose exec smtp postsuper -d ALL
```

## Check for an open relay

From a network that is not trusted by your mail server, verify that unauthenticated third-party relay is rejected. These commands must fail to relay.

```bash
swaks --server mail.myrobertson.net --port 25 \
  --from outside@example.net --to victim@example.org --quit-after RCPT

swaks --server mail.myrobertson.net --port 587 --tls \
  --from outside@example.net --to victim@example.org --quit-after RCPT
```

Expected results:

- Port 25 may accept mail only for domains hosted by Mailu, and must reject third-party recipients.
- Port 587 must require authentication before accepting mail.

## Restore service safely

1. Disable or rotate the compromised account password.
2. Purge abusive queue entries.
3. Restart SMTP:

```bash
cd /opt/mailu
sudo docker compose up -d smtp
```

4. Send one authenticated test message from a known-good account.
5. Watch logs for 15-30 minutes:

```bash
cd /opt/mailu
sudo docker compose logs -f smtp front admin
```

## Follow-up hardening

- Use unique SMTP accounts per device/application; do not share `noreply@...` broadly.
- Prefer least-privilege notification accounts and rotate app/device SMTP passwords after device compromise or firmware reset.
- Consider blocking outbound TCP/25 from all hosts except the Mailu server at the router/firewall.
- Add monitoring for sudden spikes in outbound queue depth, `status=sent`, and repeated SASL failures.
- Confirm SPF, DKIM, and DMARC still align for hosted domains after containment.
- If abuse reached external providers, reply to the provider report after the account is disabled and queued spam is removed.

## Incident note template

```text
Date/time UTC:
Report source:
Reported message ID:
Authenticated SMTP user:
Source client IP from Mailu logs:
Containment action:
Queue action:
Passwords rotated:
Open relay test result:
Follow-up tasks:
```
