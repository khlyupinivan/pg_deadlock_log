#include "postgres.h"
#include "fmgr.h"

#include "access/xact.h"
#include "utils/guc.h"
#include "utils/elog.h"

#include "pg_deadlock_log_internal.h"

PG_MODULE_MAGIC;

/* GUC-параметры — определяем здесь, объявляем в header */
bool pg_deadlock_log_enabled = true;
bool pg_deadlock_log_store_query = true;
char *pg_deadlock_log_schema = "public";

/* Старый hook */
static void (*prev_emit_log_hook)(ErrorData *edata) = NULL;

/* Флаг защиты от рекурсии */
static bool in_pg_deadlock_log = false;

/* Прототипы обязательных функций */
void _PG_init(void);
void _PG_fini(void);

static void pg_deadlock_log_emit_log(ErrorData *edata);

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

    prev_emit_log_hook = emit_log_hook;
    emit_log_hook = pg_deadlock_log_emit_log;
}

void _PG_fini(void)
{
    emit_log_hook = prev_emit_log_hook;
}

/*
 * emit_log_hook: фильтруем дедлоки и отдаём их на логирование.
 */
static void
pg_deadlock_log_emit_log(ErrorData *edata)
{
    if (prev_emit_log_hook)
        prev_emit_log_hook(edata);

    if (!pg_deadlock_log_enabled)
        return;

    if (edata == NULL || edata->elevel != ERROR)
        return;

    if (in_pg_deadlock_log)
        return;

    if (edata->sqlerrcode != ERRCODE_T_R_DEADLOCK_DETECTED)
        return;

    in_pg_deadlock_log = true;

    PG_TRY();
    {
        DeadlockLogEntry entry;

        MemSet(&entry, 0, sizeof(DeadlockLogEntry));

        pg_deadlock_log_fill_from_errordata(edata, &entry);
        pg_deadlock_log_fill_tx_info(&entry);

        if (pg_deadlock_log_fill_metadata_via_spi(&entry))
        {
            pg_deadlock_log_insert_entry(&entry);
        }
    }
    PG_CATCH();
    {
        ErrorData *c_edata = CopyErrorData();

        FlushErrorState();
        AbortOutOfAnyTransaction();

        elog(LOG,
             "pg_deadlock_log: caught ERROR in hook: sqlstate=%s, message=%s",
             unpack_sql_state(c_edata->sqlerrcode),
             c_edata->message ? c_edata->message : "<null>");

        FreeErrorData(c_edata);
    }
    PG_END_TRY();

    in_pg_deadlock_log = false;
}