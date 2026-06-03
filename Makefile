MODULE_big = pg_deadlock_log
OBJS = pg_deadlock_log.o \
       pg_deadlock_log_context.o \
       pg_deadlock_log_insert.o \
       pg_deadlock_log_worker.o

EXTENSION = pg_deadlock_log
DATA      = pg_deadlock_log--1.0.sql
REGRESS   = pg_deadlock_log

PG_CONFIG = pg_config

PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)