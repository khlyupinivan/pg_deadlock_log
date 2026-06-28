#!/usr/bin/env python3
"""
tests/benchmark_overhead.py

Измеряет overhead от pg_deadlock_log на обычные транзакции.
Сравнивает latency простых UPDATE без дедлоков — с расширением и без.

Методология:
  - Расширение нельзя выгрузить без рестарта PG, поэтому сравниваем:
    ВАРИАНТ А: расширение включено (pg_deadlock_log_enabled = on)
    ВАРИАНТ Б: расширение отключено (pg_deadlock_log_enabled = off)
  - Оба варианта гоняем на одинаковой нагрузке: N транзакций x M итераций
  - Измеряем: среднее, медиану, p95, p99, min, max
"""

import time
import statistics
import psycopg2
import random

# =============================================================================
# Конфигурация
# =============================================================================
DSN          = "host=localhost port=5432 dbname=postgres user=postgres"
WARMUP_OPS   = 2000    # прогрев — не считаем
ROUNDS    = 100        # количество раундов
ROUND_OPS = 500        # операций в каждом раунде
TABLE_ROWS   = 10000   # строк в тестовой таблице

# =============================================================================
# Утилиты
# =============================================================================
def new_conn(autocommit=False):
    conn = psycopg2.connect(DSN)
    conn.autocommit = autocommit
    return conn

def setup():
    print("=== SETUP ===")
    conn = new_conn(autocommit=True)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bench_test (
            id  INTEGER PRIMARY KEY,
            val TEXT
        )
    """)
    cur.execute("TRUNCATE bench_test")
    values = ", ".join(f"({i}, 'v{i}')" for i in range(1, TABLE_ROWS + 1))
    cur.execute(f"INSERT INTO bench_test VALUES {values}")
    cur.close()
    conn.close()
    print(f"  Таблица bench_test создана ({TABLE_ROWS} строк)")

def set_extension_enabled(enabled: bool):
    conn = new_conn(autocommit=True)
    cur = conn.cursor()
    val = "on" if enabled else "off"
    cur.execute(f"ALTER SYSTEM SET pg_deadlock_log.enabled = '{val}'")
    cur.execute("SELECT pg_reload_conf()")
    cur.close()
    conn.close()
    time.sleep(0.5)

def get_extension_status() -> str:
    conn = new_conn(autocommit=True)
    cur = conn.cursor()
    cur.execute("SHOW pg_deadlock_log.enabled")
    val = cur.fetchone()[0]
    cur.close()
    conn.close()
    return val

# =============================================================================
# Нагрузочные сценарии
# =============================================================================
def bench_simple_update(n_ops: int) -> list:
    """Простой UPDATE одной строки в транзакции."""
    conn = new_conn()
    cur = conn.cursor()
    latencies = []

    for i in range(n_ops):
        row_id = (i % TABLE_ROWS) + 1
        t0 = time.perf_counter()
        cur.execute("UPDATE bench_test SET val = %s WHERE id = %s",
                    (f'val_{i}', row_id))
        conn.commit()
        t1 = time.perf_counter()
        latencies.append((t1 - t0) * 1000)

    cur.close()
    conn.close()
    return latencies

def bench_select(n_ops: int) -> list:
    """Простой SELECT — проверяем что хук не влияет на read-only."""
    conn = new_conn(autocommit=True)
    cur = conn.cursor()
    latencies = []

    for i in range(n_ops):
        row_id = (i % TABLE_ROWS) + 1
        t0 = time.perf_counter()
        cur.execute("SELECT val FROM bench_test WHERE id = %s", (row_id,))
        cur.fetchone()
        t1 = time.perf_counter()
        latencies.append((t1 - t0) * 1000)

    cur.close()
    conn.close()
    return latencies

def bench_multi_update(n_ops: int) -> list:
    """Транзакция с несколькими UPDATE — ближе к реальной нагрузке."""
    conn = new_conn()
    cur = conn.cursor()
    latencies = []

    for i in range(n_ops):
        ids = [(i * 3 + j) % TABLE_ROWS + 1 for j in range(3)]
        t0 = time.perf_counter()
        for row_id in ids:
            cur.execute("UPDATE bench_test SET val = %s WHERE id = %s",
                        (f'val_{i}', row_id))
        conn.commit()
        t1 = time.perf_counter()
        latencies.append((t1 - t0) * 1000)

    cur.close()
    conn.close()
    return latencies

# =============================================================================
# Статистика
# =============================================================================
def calc_stats(latencies: list) -> dict:
    s = sorted(latencies)
    n = len(s)
    return {
        "mean":   statistics.mean(s),
        "median": statistics.median(s),
        "p95":    s[int(n * 0.95)],
        "p99":    s[int(n * 0.99)],
        "min":    s[0],
        "max":    s[-1],
        "stdev":  statistics.stdev(s),
    }

def print_stats(label: str, stats: dict):
    print(f"  {label}:")
    print(f"    mean={stats['mean']:.3f}ms  median={stats['median']:.3f}ms  "
          f"p95={stats['p95']:.3f}ms  p99={stats['p99']:.3f}ms")
    print(f"    min={stats['min']:.3f}ms  max={stats['max']:.3f}ms  "
          f"stdev={stats['stdev']:.3f}ms")

def print_diff(stats_on: dict, stats_off: dict):
    print(f"  Разница (enabled vs disabled):")
    all_ok = True
    for key in ("mean", "median", "p95", "p99"):
        diff = stats_on[key] - stats_off[key]
        pct  = (diff / stats_off[key]) * 100 if stats_off[key] > 0 else 0
        sign = "+" if diff >= 0 else ""
        color = "\033[31m" if pct > 10 else "\033[32m"
        mark  = "⚠" if pct > 10 else "✓"
        if pct > 10:
            all_ok = False
        print(f"    {key:8s}: {sign}{diff:.3f}ms  ({color}{sign}{pct:.1f}%\033[0m)  {mark}")
    return all_ok

# =============================================================================
# Один сценарий
# =============================================================================
def run_benchmark(name: str, bench_fn) -> bool:
    print(f"\n--- {name} ({ROUNDS} раундов × {ROUND_OPS} ops) ---")

    # Прогрев обоих вариантов
    set_extension_enabled(True)
    bench_fn(WARMUP_OPS)
    set_extension_enabled(False)
    bench_fn(WARMUP_OPS)

    results = {True: [], False: []}

    for i in range(ROUNDS):
        order = [True, False]
        random.shuffle(order)
        for enabled in order:
            set_extension_enabled(enabled)
            latencies = bench_fn(ROUND_OPS)
            results[enabled].extend(latencies)

        if (i + 1) % 10 == 0:
            print(f"  [{i + 1}/{ROUNDS}] раундов завершено...")

    for enabled in (True, False):
        label = "enabled " if enabled else "disabled"
        stats = calc_stats(results[enabled])
        print_stats(f"pg_deadlock_log {label}", stats)

    ok = print_diff(calc_stats(results[True]), calc_stats(results[False]))
    return ok

# =============================================================================
# Точка входа
# =============================================================================
if __name__ == "__main__":
    import sys

    setup()

    print(f"\n=== БЕНЧМАРК OVERHEAD pg_deadlock_log ===")
    print(f"  Прогрев: {WARMUP_OPS} ops, Замер: {ROUNDS*ROUND_OPS} ops\n")

    ok1 = run_benchmark("Simple UPDATE",      bench_simple_update)
    ok2 = run_benchmark("SELECT (read-only)", bench_select)
    ok3 = run_benchmark("Multi UPDATE (3x)",  bench_multi_update)

    # Восстанавливаем enabled=on после бенчмарка
    set_extension_enabled(True)

    print("\n=== ИТОГ ===")
    if ok1 and ok2 and ok3:
        print("  \033[32m✓ Overhead в пределах погрешности (<10%) — production-safe\033[0m")
    else:
        print("  \033[31m✗ Overhead > 10% в одном из сценариев — стоит оптимизировать\033[0m")

    print("\n  Порог 10% условный — результаты зависят от железа и окружения.")
