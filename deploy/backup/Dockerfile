# Backup and restore-test runner.
#
# Built on the SAME pgvector image as the database: pg_dump must be at least the
# server's major version, and the weekly restore test starts a throwaway server
# that has to understand `CREATE EXTENSION vector` in the dump.
#
# restic comes from Debian's signed archive (verified by apt), not a download.
FROM pgvector/pgvector:0.8.7-pg17-bookworm@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d

# gosu is the postgres image's tool for dropping root in ITS entrypoint, which
# this image replaces. It is a Go binary built with whatever Go was current
# when the base was made, so it carried most of the image's CVEs (59 of 85 in
# Trivy on 2026-10-19) while never running. Removed, not patched.
#
# `upgrade` takes Debian's security fixes at build time: the pgvector base is
# rebuilt only when pgvector releases, and lagged the security archive by 29
# fixable HIGH/CRITICAL findings when this was written. The release workflow
# builds from scratch, so every release carries the fixes current that day.
# Not version-pinned (DL3008): see backend/Dockerfile.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends restic curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /usr/local/bin/gosu

COPY common.sh backup.sh restore-test.sh restore.sh entrypoint.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/*.sh \
 && mkdir -p /backups /scratch /ops-state \
 && chown postgres:postgres /backups /scratch /ops-state

# postgres, by number (the base image's uid and gid).
USER 999:999
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["schedule"]
