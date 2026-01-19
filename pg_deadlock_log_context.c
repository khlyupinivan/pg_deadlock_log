#include "postgres.h"

#include "access/xact.h"
#include "executor/spi.h"
#include "libpq/libpq-be.h"
#include "miscadmin.h"
#include "storage/proc.h"
#include "storage/procarray.h"
#include "storage/predicate.h"
#include "tcop/tcopprot.h"
#include "utils/builtins.h"
#include "utils/elog.h"
#include "utils/guc.h"
#include "utils/snapmgr.h"

#include "pg_deadlock_log_internal.h"

extern const char *debug_query_string;

/*
 * Часть данных из ErrorData и глобального состояния backend'а.
 */
void pg_deadlock_log_fill_from_errordata(ErrorData *edata, DeadlockLogEntry *entry)
{
    entry->query_str = NULL;
    entry->error_msg = NULL;
    entry->error_detail = NULL;
    entry->sqlstate_str = NULL;
    entry->app_name = NULL;
    entry->client_addr_str = NULL;

    if (pg_deadlock_log_store_query && debug_query_string)
        entry->query_str = debug_query_string;

    if (edata != NULL)
    {
        if (edata->message)
            entry->error_msg = edata->message;
        if (edata->detail)
            entry->error_detail = edata->detail;
        if (edata->sqlerrcode)
            entry->sqlstate_str = unpack_sql_state(edata->sqlerrcode);
    }

    if (MyProcPort && MyProcPort->remote_host && MyProcPort->remote_host[0] != '\0')
        entry->client_addr_str = MyProcPort->remote_host;
}

/*
 * schema, search_path, xid, virtualxid.
 */
void pg_deadlock_log_fill_tx_info(DeadlockLogEntry *entry)
{
    TransactionId xid;
    VirtualTransactionId vxid;

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

    /* virtualxid */
    vxid.backendId = InvalidBackendId;
    vxid.localTransactionId = InvalidLocalTransactionId;

    if (MyProc != NULL)
    {
        vxid.backendId = MyProc->backendId;
        vxid.localTransactionId = MyProc->lxid;
    }

    if (VirtualTransactionIdIsValid(vxid))
        entry->vxid_str = psprintf("%d/%u", vxid.backendId, vxid.localTransactionId);

    if (entry->schema == NULL || entry->schema[0] == '\0')
        entry->schema = "public";
}

/*
 * Метаданные через SPI: current_database, current_user, application_name.
 */
bool pg_deadlock_log_fill_metadata_via_spi(DeadlockLogEntry *entry)
{
    int ret;
    char *app_name_sql = NULL;

    entry->db_name = NULL;
    entry->user_name = NULL;
    entry->app_name = NULL;

    AbortOutOfAnyTransaction();
    StartTransactionCommand();
    PushActiveSnapshot(GetTransactionSnapshot());

    if (SPI_connect() != SPI_OK_CONNECT)
    {
        elog(WARNING, "pg_deadlock_log: SPI_connect failed in hook (metadata phase)");
        PopActiveSnapshot();
        AbortCurrentTransaction();
        return false;
    }

    /* database_name */
    ret = SPI_execute("SELECT current_database()", true, 1);
    if (ret == SPI_OK_SELECT && SPI_processed > 0)
        entry->db_name = SPI_getvalue(SPI_tuptable->vals[0],
                                      SPI_tuptable->tupdesc,
                                      1);
    if (entry->db_name == NULL)
        entry->db_name = "<unknown_db>";

    /* user_name */
    ret = SPI_execute("SELECT current_user", true, 1);
    if (ret == SPI_OK_SELECT && SPI_processed > 0)
        entry->user_name = SPI_getvalue(SPI_tuptable->vals[0],
                                        SPI_tuptable->tupdesc,
                                        1);
    if (entry->user_name == NULL)
        entry->user_name = "<unknown_user>";

    /* application_name */
    ret = SPI_execute("SELECT current_setting('application_name', true)", true, 1);
    if (ret == SPI_OK_SELECT && SPI_processed > 0)
        app_name_sql = SPI_getvalue(SPI_tuptable->vals[0],
                                    SPI_tuptable->tupdesc,
                                    1);
    if (app_name_sql != NULL && app_name_sql[0] != '\0')
        entry->app_name = app_name_sql;

    SPI_finish();
    PopActiveSnapshot();
    CommitTransactionCommand();

    return true;
}