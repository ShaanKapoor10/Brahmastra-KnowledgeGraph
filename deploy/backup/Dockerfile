# Backup and restore-test runner.
#
# Built on the SAME pgvector image as the database: pg_dump must be at least the
# server's major version, and the weekly restore test starts a throwaway server
# that has to understand `CREATE EXTENSION vector` in the dump.
#
# restic comes from Debian's signed archive (verified by apt), not a download.
FROM pgvector/pgvector:0.8.0-pg17

RUN apt-get update \
 && apt-get install -y --no-install-recommends restic curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY common.sh backup.sh restore-test.sh restore.sh entrypoint.sh /usr/local/bin/
RUN chmod 0755 /usr/local/bin/*.sh \
 && mkdir -p /backups /scratch \
 && chown postgres:postgres /backups /scratch

USER postgres
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["schedule"]
