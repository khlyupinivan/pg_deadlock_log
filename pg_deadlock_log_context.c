#include "postgres.h"

#include "access/xact.h"
#include "executor/spi.h"
#include "libpq/libpq-be.h"
#include "miscadmin.h"
#include "storage/lock.h"
#include "storage/lmgr.h"
#include "storage/proc.h"
#include "storage/procarray.h"
#include "storage/predicate.h"
#include "tcop/tcopprot.h"
#include "utils/builtins.h"
#include "utils/elog.h"
#include "utils/guc.h"
#include "utils/snapmgr.h"

#include "pg_deadlock_log_internal.h"

/* Временное объявление EDGE для каста */
/* В реальности определяется в deadlock.c */
typedef struct EDGE
{
    PGPROC *waiter;
    PGPROC *blocker;
    LOCK   *lock;
    int     pred;
    int     ink;
} EDGE;

extern const char *debug_query_string;

/*
 * Заполнение полей из DeadlockInfo.
 */
void
pg_deadlock_log_fill_from_deadlockinfo(const DeadlockInfo *info,
                                       DeadlockLogEntry *entry)
{
    /* Основная информация из DeadlockInfo */
    entry->victim_pid   = info->victim_proc ? info->victim_proc->pid : 0;
    entry->occurred_at  = info->snapshot_time;

    /* Пока оставляем NULL — заполняются из других источников */
    entry->query_str        = NULL;
    entry->error_msg        = NULL;
    entry->error_detail     = NULL;
    entry->sqlstate_str     = "40P01";     /* дедлок — всегда 40P01 */
    entry->app_name         = NULL;
    entry->client_addr_str  = NULL;

    /* client_addr из MyProcPort (контекст жертвы — текущий backend) */
    if (MyProcPort && MyProcPort->remote_host && MyProcPort->remote_host[0] != '\0')
        entry->client_addr_str = MyProcPort->remote_host;
}

/*
 * Извлекает PID всех участников дедлока из DeadlockInfo.
 */
void
pg_deadlock_log_fill_participants(const DeadlockInfo *info,
                                  DeadlockLogEntry *entry)
{
    entry->all_pids   = NULL;
    entry->n_all_pids = 0;

    if (info == NULL || info->n_procs <= 0 || info->all_procs == NULL)
        return;

    /* Выделяем массив под PID */
    entry->all_pids = (int *) palloc(info->n_procs * sizeof(int));
    entry->n_all_pids = info->n_procs;

    /* Заполняем PID из PGPROC */
    for (int i = 0; i < info->n_procs; i++)
    {
        if (info->all_procs[i] != NULL)
            entry->all_pids[i] = info->all_procs[i]->pid;
        else
            entry->all_pids[i] = 0;
    }
}

/*
 * Строит текстовое описание графа блокировок.
 * Формат: "X waits for Y on <lockdesc>; Y waits for Z on <lockdesc>"
 */
void
pg_deadlock_log_fill_lock_cycle(const DeadlockInfo *info,
                                DeadlockLogEntry *entry)
{
    StringInfoData buf;
    int            i;

    entry->lock_cycle = NULL;

    if (info == NULL || info->n_procs <= 0 || info->all_procs == NULL)
        return;

    initStringInfo(&buf);

    /* Строим цикл из all_procs: каждый ждёт следующего */
    for (i = 0; i < info->n_procs; i++)
    {
        PGPROC *waiter  = info->all_procs[i];
        PGPROC *blocker = info->all_procs[(i + 1) % info->n_procs];

        int waiter_pid  = waiter  ? waiter->pid  : 0;
        int blocker_pid = blocker ? blocker->pid : 0;

        if (i > 0)
            appendStringInfoString(&buf, "; ");

        appendStringInfo(&buf, "%d waits for %d", waiter_pid, blocker_pid);
    }

    entry->lock_cycle = buf.data;
    
    elog(LOG, "pg_deadlock_log: lock_cycle built: %s", entry->lock_cycle);
}

/*
 * Читает запросы участников через pg_stat_activity (SPI).
 * Опционально — если нужны не только PID, но и запросы.
 */
void
pg_deadlock_log_fill_participant_queries(const DeadlockInfo *info,
                                         DeadlockLogEntry *entry)
{
    /* Пока заглушка */
    /*
     * Идея:
     * 1. Построить список PID через SPI в строку: "1,2,3"
     * 2. SELECT pid, query FROM pg_stat_activity WHERE pid = ANY(ARRAY[...])
     * 3. Заполнить массив строк в entry->all_queries[]
     */
}

/*
 * schema, search_path, xid, virtualxid.
 */
void pg_deadlock_log_fill_tx_info(DeadlockLogEntry *entry)
{
    TransactionId xid;
    
    entry->schema = pg_deadlock_log_schema;
    entry->search_path_str = NULL;
    entry->xid_str = NULL;
    entry->vxid_str = NULL;

    /* search_path */
    PG_TRY();
    {
        entry->search_path_str = GetConfigOption("search_path", true, false);
    }
    PG_CATCH();
    {
        FlushErrorState();
        entry->search_path_str = NULL;
    }
    PG_END_TRY();

    /* xid */
    xid = GetTopTransactionIdIfAny();
    if (!TransactionIdIsValid(xid))
        xid = GetCurrentTransactionIdIfAny();

    if (TransactionIdIsValid(xid))
        entry->xid_str = psprintf("%u", xid);

    /* virtualxid — в этой версии PG используется procNumber, а не backendId */
    if (MyProc != NULL)
    {
        entry->vxid_str = psprintf("%d/%u",
                                   MyProc->vxid.procNumber,
                                   MyProc->vxid.lxid);
    }

    if (entry->schema == NULL || entry->schema[0] == '\0')
        entry->schema = "public";
}

/*
 * Метаданные через SPI: current_database, current_user, application_name.
 * Заглушка, SPI в хуке - не безопасно.
 */
bool pg_deadlock_log_fill_metadata_via_spi(DeadlockLogEntry *entry)
{
    entry->db_name = NULL;
    entry->user_name = NULL;
    entry->app_name = NULL;
    return true;
}