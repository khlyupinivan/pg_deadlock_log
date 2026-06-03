#include "postgres.h"

#include "access/xact.h"
#include "executor/spi.h"
#include "lib/stringinfo.h"
#include "miscadmin.h"
#include "pgstat.h"
#include "postmaster/bgworker.h"
#include "storage/ipc.h"
#include "storage/latch.h"
#include "storage/lwlock.h"
#include "storage/proc.h"
#include "storage/shmem.h"
#include "tcop/tcopprot.h"
#include "utils/builtins.h"
#include "utils/elog.h"
#include "utils/ps_status.h"
#include "utils/snapmgr.h"

#include "pg_deadlock_log_internal.h"

static volatile sig_atomic_t got_sigterm = false;

static void
pg_deadlock_log_sigterm(SIGNAL_ARGS)
{
    got_sigterm = true;
    SetLatch(MyLatch);
}

static void
pg_deadlock_log_do_insert(DeadlockLogShm *snap)
{
    StringInfoData buf;
    StringInfoData pids_buf;
    int            ret;
    int            i;

    initStringInfo(&buf);

    /* Строим all_pids массив */
    initStringInfo(&pids_buf);
    if (snap->n_all_pids > 0)
    {
        appendStringInfoString(&pids_buf, "ARRAY[");
        for (i = 0; i < snap->n_all_pids; i++)
        {
            if (i > 0)
                appendStringInfoString(&pids_buf, ", ");
            appendStringInfo(&pids_buf, "%d", snap->all_pids[i]);
        }
        appendStringInfoString(&pids_buf, "]");
    }
    else
        appendStringInfoString(&pids_buf, "ARRAY[]::integer[]");

    appendStringInfo(&buf,
        "INSERT INTO %s.pg_deadlock_log "
        "(occurred_at, database_name, user_name, pid_victim, "
        " sqlstate, query, error_message, error_detail, "
        " application_name, client_addr, "
        " search_path, xid, virtualxid, "
        " all_pids, lock_cycle) "
        "VALUES (now(), %s, %s, %d, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        quote_identifier(snap->schema[0] ? snap->schema : "public"),
        quote_literal_cstr(snap->db_name),
        quote_literal_cstr(snap->user_name),
        snap->victim_pid,
        quote_literal_cstr(snap->sqlstate_str[0] ? snap->sqlstate_str : "40P01"),
        quote_literal_cstr(snap->query_str),
        quote_literal_cstr(snap->error_msg[0] ? snap->error_msg : "deadlock detected"),
        quote_literal_cstr(""),
        quote_literal_cstr(snap->app_name),
        quote_literal_cstr(snap->client_addr_str),
        quote_literal_cstr(snap->search_path_str),
        quote_literal_cstr(snap->xid_str),
        quote_literal_cstr(snap->vxid_str),
        pids_buf.data,
        quote_literal_cstr(snap->lock_cycle));

    ret = SPI_execute(buf.data, false, 0);

    if (ret != SPI_OK_INSERT)
        elog(WARNING, "pg_deadlock_log: SPI INSERT failed, ret=%d", ret);
    else
        elog(LOG, "pg_deadlock_log: INSERT OK, victim_pid=%d", snap->victim_pid);

    pfree(buf.data);
    pfree(pids_buf.data);
}

PGDLLEXPORT void
pg_deadlock_log_worker_main(Datum main_arg)
{
    DeadlockLogShm snap;

    pqsignal(SIGTERM, pg_deadlock_log_sigterm);
    BackgroundWorkerUnblockSignals();

    PG_TRY();
    {
        BackgroundWorkerInitializeConnection("postgres", NULL, 0);
    }
    PG_CATCH();
    {
        ErrorData *edata = CopyErrorData();
        FlushErrorState();
        FreeErrorData(edata);
        proc_exit(1);
    }
    PG_END_TRY();

    elog(LOG, "pg_deadlock_log worker started");

    while (!got_sigterm)
    {
        int rc;

        rc = WaitLatch(MyLatch,
                       WL_LATCH_SET | WL_TIMEOUT | WL_EXIT_ON_PM_DEATH,
                       50000L,
                       PG_WAIT_EXTENSION);

        ResetLatch(MyLatch);

        if (rc & WL_POSTMASTER_DEATH)
            proc_exit(1);

        if (got_sigterm)
            break;

        /* Проверяем shared memory */
        if (pg_deadlock_shm == NULL)
            continue;

        LWLockAcquire(&pg_deadlock_shm->lock, LW_EXCLUSIVE);

        if (!pg_deadlock_shm->pending)
        {
            LWLockRelease(&pg_deadlock_shm->lock);
            continue;
        }

        /* Копируем локально и сбрасываем флаг */
        memcpy(&snap, pg_deadlock_shm, sizeof(DeadlockLogShm));
        pg_deadlock_shm->pending = false;

        LWLockRelease(&pg_deadlock_shm->lock);

        /* Делаем INSERT через SPI */
        SetCurrentStatementStartTimestamp();
        StartTransactionCommand();
        SPI_connect();
        PushActiveSnapshot(GetTransactionSnapshot());

        PG_TRY();
        {
            pg_deadlock_log_do_insert(&snap);
        }
        PG_CATCH();
        {
            ErrorData *edata = CopyErrorData();
            FlushErrorState();
            elog(WARNING, "pg_deadlock_log worker: insert error: %s",
                 edata->message ? edata->message : "<null>");
            FreeErrorData(edata);
        }
        PG_END_TRY();

        PopActiveSnapshot();
        SPI_finish();
        CommitTransactionCommand();
    }

    elog(LOG, "pg_deadlock_log worker shutting down");
    proc_exit(0);
}