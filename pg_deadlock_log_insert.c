#include "postgres.h"

#include "access/xact.h"
#include "executor/spi.h"
#include "lib/stringinfo.h"
#include "miscadmin.h"
#include "utils/builtins.h"
#include "utils/elog.h"
#include "utils/snapmgr.h"

#include "pg_deadlock_log_internal.h"

/* Локальный helper: формирование INSERT SQL в StringInfo */
static void
pg_deadlock_log_build_insert_sql(StringInfo buf,
                                 const DeadlockLogEntry *e)
{
    const char *schema = e->schema;
    const char *db_name = e->db_name;
    const char *user_name = e->user_name;
    const char *sqlstate_str = e->sqlstate_str;
    const char *query_str = e->query_str;
    const char *error_msg = e->error_msg;
    const char *error_detail = e->error_detail;
    const char *app_name = e->app_name;
    const char *client_addr = e->client_addr_str;
    const char *search_path = e->search_path_str;
    const char *xid_str = e->xid_str;
    const char *vxid_str = e->vxid_str;

    if (!error_msg)
        error_msg = "deadlock detected";
    if (!error_detail)
        error_detail = "";
    if (!query_str)
        query_str = "";
    if (!app_name)
        app_name = "";
    if (!client_addr)
        client_addr = "";
    if (!sqlstate_str)
        sqlstate_str = "40P01";
    if (!schema || schema[0] == '\0')
        schema = "public";
    if (!search_path)
        search_path = "";
    if (!xid_str)
        xid_str = "";
    if (!vxid_str)
        vxid_str = "";

    if (!db_name)
        db_name = "<unknown_db>";
    if (!user_name)
        user_name = "<unknown_user>";

    initStringInfo(buf);

    elog(LOG,
         "pg_deadlock_log: debug app_name='%s', client_addr='%s'",
         app_name ? app_name : "<null>",
         client_addr ? client_addr : "<null>");

    appendStringInfo(
        buf,
        "INSERT INTO %s.pg_deadlock_log "
        "(occurred_at, database_name, user_name, pid_victim, "
        " sqlstate, query, error_message, error_detail, application_name, client_addr, "
        " search_path, xid, virtualxid) "
        "VALUES (now(), "
        " %s, %s, %d, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
        quote_identifier(schema),
        quote_literal_cstr(db_name),
        quote_literal_cstr(user_name),
        MyProcPid,
        quote_literal_cstr(sqlstate_str),
        quote_literal_cstr(query_str),
        quote_literal_cstr(error_msg),
        quote_literal_cstr(error_detail),
        quote_literal_cstr(app_name),
        quote_literal_cstr(client_addr),
        quote_literal_cstr(search_path),
        quote_literal_cstr(xid_str),
        quote_literal_cstr(vxid_str));
}

/*
 * Строим SQL и выполняем INSERT в отдельной транзакции.
 */
void pg_deadlock_log_insert_entry(const DeadlockLogEntry *entry)
{
    StringInfoData buf;
    int ret;

    pg_deadlock_log_build_insert_sql(&buf, entry);

    elog(LOG, "pg_deadlock_log: SQL = %s", buf.data);

    AbortOutOfAnyTransaction();
    StartTransactionCommand();
    PushActiveSnapshot(GetTransactionSnapshot());

    if (SPI_connect() != SPI_OK_CONNECT)
    {
        elog(WARNING, "pg_deadlock_log: SPI_connect failed in hook (insert phase)");
        PopActiveSnapshot();
        AbortCurrentTransaction();
        pfree(buf.data);
        return;
    }

    ret = SPI_execute(buf.data, false, 0);
    if (ret != SPI_OK_INSERT)
        elog(WARNING, "pg_deadlock_log: SPI_execute failed in hook, rc = %d", ret);

    SPI_finish();
    PopActiveSnapshot();
    CommitTransactionCommand();

    pfree(buf.data);
}