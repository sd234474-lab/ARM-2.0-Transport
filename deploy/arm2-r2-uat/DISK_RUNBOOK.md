# Issue 81: private DOCUMENT storage on the existing R2

The optional `compose.arm2-r2-disk.yaml` overlay retains the original three
containers and volumes and adds one project-owned TLS tunnel gateway. It does not
change the B5 logic, business schema, users, passwords, origin, or loopback port.
Start from the currently installed reviewed B5 revision; do not downgrade to an
older SHA from the initial Issue 81 description.

## Required private preflight

Before copying any credential to Ubuntu, decrypt the owner's existing DPAPI
SecureString only in memory on its original Windows account. Use read-only API
calls to verify the user UID against the owner-confirmed test folder resource ID,
resolve its internal path with authenticated resources metadata, and verify that
the DOCUMENT root is disjoint from photo/exchange/training/spam roots. Obtain an
upload URL only to check permissions and its hostname; do not log that URL.

The folder must be private before any original is uploaded. After verifying
ownership, remove its public publication through `/resources/unpublish` and
verify that its authenticated metadata no longer has `public_key`. Public URLs
are never DOCUMENT storage references.

## Credential delivery and configuration

Use SSH stdin, without a token in arguments, shell command text, logs, Git, or
images. A short-lived project-scoped helper with `--network none` writes the
token to the private directory `/home/arm2admin/.config/arm2-r2-disk`.
Its `token` is a regular file owned by service UID 10001, mode 0600; the containing
directory remains owned by the deployment user, mode 0700. Bind only this file,
read-only, to the API. Do not mount it in the web or gateway container.

`config.json` in that same directory contains only `account_uid`, `resource_id`,
`document_root`, and `allowed_hosts`. It contains no token or signed URL. Its
hostname list consists of `cloud-api.yandex.net` and upload/download hosts
returned by the test Disk API. A verified `uploader` host enables only Yandex
Disk's rotating `uploader[a-z0-9]{1,24}.disk.yandex.net` pool: separate link
requests return different nodes. There is no broad `*.yandex.net` rule. It must be readable by
the unprivileged service and not writable by group/other users.

Keep the existing base env-file and its empty `YANDEX_DISK_TOKEN`. The R2-only
launcher reads the private mounted file into process memory, verifies test
identity, root ownership and private publication state over the gateway, and
executes the unchanged application command. Docker Config.Env stays token-free.
Never print a full inspect/config, process environment, OAuth header, or provider
exception. The existing YandexDisk adapter handles upload/read and SHA validation.

## Isolation and launch

The gateway has no published port, no token, and no DB/runtime volumes. It is
unprivileged, drops all capabilities, uses no-new-privileges, and has a read-only
root filesystem. Only it joins `r2_disk_egress`. API and DB stay on `r2_internal`;
the web retains only its original networks and loopback port. No host firewall,
NAT, Windows forwarding, B1, or other project changes are required.

The gateway accepts only bounded CONNECT requests to configured Disk endpoints
and the narrowly validated upload pool
on TCP 443, rejects private/loopback/link-local resolutions, pins the checked
destination IP, and relays TLS without decrypting or logging it. The existing
adapter keeps TLS certificate verification and URL/redirect/content/SHA checks.
Unknown future provider hostnames fail closed and require another verified
configuration update. Unrelated sites and raw direct API egress must fail.

Take a private, current SQL + both runtime/FILE volume backup and configuration
snapshot first; verify archives and checksums. Keep the prior Issue 80 backup.
Build both SHA-tagged images, validate the merged Compose config, bring up the
gateway and only API/WEB with both Compose files, and run:

```
python3 deploy/arm2-r2-uat/preflight.py --env-file <existing-private-env> --phase ready --disk
```

The explicit Disk mode validates the overlay, then applies the unchanged base
guards to its verified projection. It additionally checks mounts, private token
ownership/mode, gateway confinement, no foreign egress consumers, exact Alembic
0071, and all healthchecks. No migrations, reset, user provisioning, or DB restart.

## Live acceptance and rollback

Through the existing operator DOCUMENT UI, upload one synthetic small PDF, open
its staged preview, save and accept it, close/reopen, and download it. Compare
SHA-256 to the original. Verify unauthenticated denial and that no browser request
uses OAuth, public or signed provider URLs. Restart only API/WEB with the overlay,
repeat reopen/download and byte comparison. Leave the test original as evidence.
Report owner-interactive login separately when it has not been observed.

For rollback stop/remove only this project's gateway and recreate only R2 API/WEB
using the saved prior env, checkout SHA, and original Compose file without the
overlay. Keep all three named volumes and the DB container. Do not reset, restore
over working data, delete the uploaded proof, republish the folder, or change B1.
Retain the private backup, configuration, exact image tags, and evidence hashes.
