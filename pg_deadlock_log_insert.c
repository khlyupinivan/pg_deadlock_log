#include "postgres.h"

#include "storage/lwlock.h"
#include "utils/elog.h"

#include "pg_deadlock_log_internal.h"

/*
 * Записывает DeadlockLogEntry в shared memory.
 * Вызывается из deadlock_hook — без блокирующих операций!
 */
void
pg_deadlock_log_write_shm(const DeadlockLogEntry *entry)
{
    DeadlockLogShm *shm = pg_deadlock_shm;
    int i;

    if (shm == NULL)
    {
        elog(LOG, "pg_deadlock_log: shared memory not initialized");
        return;
    }

    LWLockAcquire(&shm->lock, LW_EXCLUSIVE);

    /* Если предыдущая запись ещё не обработана — перезаписываем */
    shm->pending    = false;
    shm->victim_pid = entry->victim_pid;

    /* all_pids */
    shm->n_all_pids = 0;
    if (entry->all_pids && entry->n_all_pids > 0)
    {
        int n = entry->n_all_pids;
        if (n > DEADLOCK_MAX_PIDS)
            n = DEADLOCK_MAX_PIDS;
        for (i = 0; i < n; i++)
            shm->all_pids[i] = entry->all_pids[i];
        shm->n_all_pids = n;
    }

#define COPY_STR(dst, src) \
    do { \
        if (src && (src)[0] != '\0') \
            strlcpy(dst, src, sizeof(dst)); \
        else \
            dst[0] = '\0'; \
    } while(0)

    COPY_STR(shm->lock_cycle,      entry->lock_cycle);
    COPY_STR(shm->query_str,       entry->query_str);
    COPY_STR(shm->error_msg,       entry->error_msg);
    COPY_STR(shm->sqlstate_str,    entry->sqlstate_str);
    COPY_STR(shm->app_name,        entry->app_name);
    COPY_STR(shm->client_addr_str, entry->client_addr_str);
    COPY_STR(shm->db_name,         entry->db_name);
    COPY_STR(shm->user_name,       entry->user_name);
    COPY_STR(shm->schema,          entry->schema);
    COPY_STR(shm->search_path_str, entry->search_path_str);
    COPY_STR(shm->xid_str,         entry->xid_str);
    COPY_STR(shm->vxid_str,        entry->vxid_str);

    shm->pending = true;

    LWLockRelease(&shm->lock);

    if (shm->worker_latch != NULL)
        SetLatch(shm->worker_latch);

    elog(LOG, "pg_deadlock_log: written to shm, victim_pid=%d", entry->victim_pid);
}