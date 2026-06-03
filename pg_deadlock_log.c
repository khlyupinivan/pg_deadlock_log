#include "postgres.h"
#include "fmgr.h"

#include "access/xact.h"
#include "libpq/libpq-be.h"
#include "miscadmin.h"
#include "postmaster/bgworker.h"
#include "storage/ipc.h"
#include "storage/lock.h"
#include "storage/lwlock.h"
#include "storage/shmem.h"
#include "tcop/tcopprot.h"
#include "utils/errcodes.h"
#include "utils/elog.h"
#include "utils/guc.h"

#include "pg_deadlock_log_internal.h"

PG_MODULE_MAGIC;


/* GUC-параметры — определяем здесь, объявляем в header */
bool pg_deadlock_log_enabled = true;
bool pg_deadlock_log_store_query = true;
char *pg_deadlock_log_schema = "public";
int pg_deadlock_log_worker_timeout = 50000;

/* Предыдущий deadlock-хук для цепочки вызовов */
DeadlockLogShm *pg_deadlock_shm = NULL;
static deadlock_log_hook_type prev_deadlock_hook = NULL;

/* Флаг защиты от рекурсии */
static bool in_deadlock_hook = false;

/* Прототипы обязательных функций */
void _PG_init(void);
void _PG_fini(void);

static void pg_deadlock_log_hook(const DeadlockInfo *info);
static void pg_deadlock_log_shmem_request(void);
static void pg_deadlock_log_shmem_startup(void);

static shmem_request_hook_type  prev_shmem_request_hook  = NULL;
static shmem_startup_hook_type  prev_shmem_startup_hook  = NULL;

static void
pg_deadlock_log_shmem_request(void)
{
    if (prev_shmem_request_hook)
        prev_shmem_request_hook();
    RequestAddinShmemSpace(sizeof(DeadlockLogShm));
    RequestNamedLWLockTranche("pg_deadlock_log", 1);
}

static void
pg_deadlock_log_shmem_startup(void)
{
    bool found;

    if (prev_shmem_startup_hook)
        prev_shmem_startup_hook();

    LWLockAcquire(AddinShmemInitLock, LW_EXCLUSIVE);

    pg_deadlock_shm = ShmemInitStruct("pg_deadlock_log",
                                      sizeof(DeadlockLogShm),
                                      &found);
    if (!found)
    {
        MemSet(pg_deadlock_shm, 0, sizeof(DeadlockLogShm));
        pg_deadlock_shm->pending = false;
    }

    LWLockRelease(AddinShmemInitLock);
}

void _PG_init(void)
{
    DefineCustomBoolVariable("pg_deadlock_log.enabled",
                             "Enable logging deadlocks into pg_deadlock_log table.",
                             NULL,
                             &pg_deadlock_log_enabled,
                             true,
                             PGC_SUSET,
                             0,
                             NULL, NULL, NULL);

    DefineCustomBoolVariable("pg_deadlock_log.store_query",
                             "Store victim query text in pg_deadlock_log.",
                             NULL,
                             &pg_deadlock_log_store_query,
                             true,
                             PGC_SUSET,
                             0,
                             NULL, NULL, NULL);

    DefineCustomStringVariable("pg_deadlock_log.schema",
                               "Schema for pg_deadlock_log table.",
                               NULL,
                               &pg_deadlock_log_schema,
                               "public",
                               PGC_SUSET,
                               0,
                               NULL, NULL, NULL);

    DefineCustomIntVariable("pg_deadlock_log.worker_timeout",
                            "BGWorker latch timeout in milliseconds.",
                            NULL,
                            &pg_deadlock_log_worker_timeout,
                            50000,   /* default: 50s */
                            100,     /* min: 100ms */
                            300000,  /* max: 5min */
                            PGC_SUSET,
                            0,
                            NULL, NULL, NULL);

    /* Shared memory */
    prev_shmem_request_hook  = shmem_request_hook;
    shmem_request_hook       = pg_deadlock_log_shmem_request;

    prev_shmem_startup_hook  = shmem_startup_hook;
    shmem_startup_hook       = pg_deadlock_log_shmem_startup;

    /* Deadlock hook */
    prev_deadlock_hook = deadlock_log_hook;
    deadlock_log_hook  = pg_deadlock_log_hook;

    /* Регистрируем BGWorker */
    {
        BackgroundWorker worker;
        MemSet(&worker, 0, sizeof(worker));
        snprintf(worker.bgw_name,          BGW_MAXLEN, "pg_deadlock_log worker");
        snprintf(worker.bgw_type,          BGW_MAXLEN, "pg_deadlock_log");
        snprintf(worker.bgw_library_name,  BGW_MAXLEN, "pg_deadlock_log");
        snprintf(worker.bgw_function_name, BGW_MAXLEN, "pg_deadlock_log_worker_main");
        worker.bgw_flags       = BGWORKER_SHMEM_ACCESS |
                                 BGWORKER_BACKEND_DATABASE_CONNECTION;
        worker.bgw_start_time  = BgWorkerStart_RecoveryFinished;
        worker.bgw_restart_time = 1;
        worker.bgw_main_arg    = (Datum) 0;
        RegisterBackgroundWorker(&worker);
    }

}

void _PG_fini(void)
{
    deadlock_log_hook   = prev_deadlock_hook;
    shmem_startup_hook  = prev_shmem_startup_hook;
    shmem_request_hook  = prev_shmem_request_hook;
}

/*
 * deadlock_log_hook: вызывается из DeadLockCheck() в момент,
 * когда дедлок обнаружен, жертва выбрана, но ещё не убита.
 *
 * В этом хуке у нас есть доступ к:
 *   - info->victim_proc   (PGPROC жертвы)
 *   - info->all_procs[]   (все участники цикла)
 *   - info->cycle_edges[] (ребра графа)
 *   - info->n_procs, info->n_cycle_edges
 */
static void pg_deadlock_log_hook(const DeadlockInfo *info)
{
    DeadlockLogEntry entry;

    /* Сначала передаём управление предыдущему хуку в цепочке */
    if (prev_deadlock_hook)
        prev_deadlock_hook(info);

    if (!pg_deadlock_log_enabled)
        return;

    /* Защита от рекурсии */
    if (in_deadlock_hook)
        return;

    if (info == NULL)
        return;

    in_deadlock_hook = true;

    PG_TRY();
    {
        MemSet(&entry, 0, sizeof(DeadlockLogEntry));

        /* 1. Заполняем из DeadlockInfo*/
        pg_deadlock_log_fill_from_deadlockinfo(info, &entry);
        pg_deadlock_log_fill_participants(info, &entry);
        pg_deadlock_log_fill_lock_cycle(info, &entry);
        pg_deadlock_log_fill_tx_info(&entry);

        if (pg_deadlock_log_store_query && debug_query_string != NULL)
            entry.query_str = debug_query_string;

        if (entry.db_name == NULL && MyProcPort != NULL)
            entry.db_name = MyProcPort->database_name;

        if (entry.user_name == NULL && MyProcPort != NULL)
            entry.user_name = MyProcPort->user_name;

        if (entry.app_name == NULL)
            entry.app_name = application_name;

        /* Пишем в shared memory — BGWorker подхватит */
        pg_deadlock_log_write_shm(&entry);

        if (entry.all_pids)   pfree(entry.all_pids);
        if (entry.lock_cycle) pfree(entry.lock_cycle);
        if (entry.xid_str)    pfree(entry.xid_str);
        if (entry.vxid_str)   pfree(entry.vxid_str);
    }
    PG_CATCH();
    {
        ErrorData *edata;

        if (entry.all_pids)   pfree(entry.all_pids);
        if (entry.lock_cycle) pfree(entry.lock_cycle);
        if (entry.xid_str)    pfree(entry.xid_str);
        if (entry.vxid_str)   pfree(entry.vxid_str);

        edata = CopyErrorData();
        FlushErrorState();
        elog(LOG, "pg_deadlock_log: error in hook: %s",
             edata->message ? edata->message : "<null>");
        FreeErrorData(edata);
    }
    PG_END_TRY();

    in_deadlock_hook = false;
}