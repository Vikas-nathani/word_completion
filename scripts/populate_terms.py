import os
import psycopg2
import mysql.connector
import time
# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
PG_CONFIG = {
    "host":     os.getenv("PG_HOST", "localhost"),
    "port":     int(os.getenv("PG_PORT", "5432")),
    "dbname":   os.getenv("PG_DBNAME", "umls_db"),
    "user":     os.getenv("PG_USER", "postgres"),
    "password": os.getenv("PG_PASSWORD", ""),
}

MYSQL_CONFIG = {
    "host":     "localhost",
    "port":     3306,
    "database": "umls_db",
    "user":     "umls_user",
    "password": "umls_password",
}

BATCH_SIZE = 10000

# ─────────────────────────────────────────
# STEP 1 — Connect to MySQL and load stn_id map
# ─────────────────────────────────────────
print("Connecting to MySQL...")
my_conn = mysql.connector.connect(**MYSQL_CONFIG)
my_cur = my_conn.cursor()
print("Connected to MySQL ✅")

print("\nLoading stn_tree map from MySQL...")
my_cur.execute("SELECT stn_path, stn_id FROM stn_tree")
stn_id_map = {row[0]: row[1] for row in my_cur.fetchall()}
print(f"Loaded {len(stn_id_map)} STN path → stn_id mappings ✅")

# ─────────────────────────────────────────
# STEP 2 — Connect to PostgreSQL
# ─────────────────────────────────────────
print("\nConnecting to PostgreSQL...")
pg_conn = psycopg2.connect(**PG_CONFIG)
pg_cur = pg_conn.cursor(name="terms_cursor")  # server-side cursor for large result
pg_cur.itersize = BATCH_SIZE
print("Connected to PostgreSQL ✅")

# ─────────────────────────────────────────
# STEP 3 — Execute the big join query
# ─────────────────────────────────────────
print("\nExecuting join query on PostgreSQL...")
pg_cur.execute("""
    SELECT DISTINCT ON (tm.id)
        tm.id,
        tm.term,
        tm.term_lower,
        tm.is_abbreviation,
        tm.tty,
        at.term_id,
        at.concept_id,
        at.semantic_type,
        at.stn,
        at.source,
        at.code
    FROM terms_master tm
    LEFT JOIN autocomplete_terms at ON tm.term = at.term
    ORDER BY tm.id,
        CASE at.tty
            WHEN 'PT' THEN 1
            WHEN 'PN' THEN 2
            WHEN 'SY' THEN 3
            WHEN 'FN' THEN 4
            WHEN 'AB' THEN 5
            ELSE 6
        END
""")
print("Query executed ✅")

# ─────────────────────────────────────────
# STEP 4 — Fetch in batches and insert into MySQL
# ─────────────────────────────────────────
insert_sql = """
    INSERT INTO terms (
        id, term, term_lower, is_abbreviation, tty,
        term_id, concept_id, semantic_type, stn,
        source, code, stn_id
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

total_inserted = 0
batch_number = 0
null_stn_count = 0

print(f"\nInserting into MySQL in batches of {BATCH_SIZE}...")
total_terms = 4271299
start_time = time.time()
while True:
    rows = pg_cur.fetchmany(BATCH_SIZE)
    if not rows:
        break

    batch_number += 1
    batch_to_insert = []

    for row in rows:
        (id_, term, term_lower, is_abbreviation, tty,
         term_id, concept_id, semantic_type, stn,
         source, code) = row

        # Skip terms containing @ — ICD10PCS hierarchy paths, gene locus symbols,
        # and other @ notation are unreachable by prefix search in normal clinical use
        if term_lower and '@' in term_lower:
            continue

        # Lookup stn_id from map
        stn_id = stn_id_map.get(stn) if stn else None
        if stn_id is None:
            null_stn_count += 1

        batch_to_insert.append((
            id_, term, term_lower, is_abbreviation, tty,
            term_id, concept_id, semantic_type, stn,
            source, code, stn_id
        ))

    my_cur.executemany(insert_sql, batch_to_insert)
    my_conn.commit()
    total_inserted += len(batch_to_insert)

    if batch_number % 10 == 0:
        elapsed = time.time() - start_time
        percentage = (total_inserted / total_terms) * 100
        rows_per_sec = total_inserted / elapsed if elapsed > 0 else 0
        remaining = (total_terms - total_inserted) / rows_per_sec if rows_per_sec > 0 else 0
        mins_remaining = remaining / 60
        print(f"  [{percentage:.1f}%] {total_inserted:,} / {total_terms:,} rows | "
            f"{rows_per_sec:,.0f} rows/sec | "
            f"~{mins_remaining:.1f} mins remaining")

print(f"\nTotal inserted: {total_inserted:,} rows ✅")
print(f"Rows with null stn_id: {null_stn_count:,}")

# ─────────────────────────────────────────
# STEP 5 — Verify
# ─────────────────────────────────────────
print("\nVerifying...")

my_cur.execute("SELECT COUNT(*) FROM terms")
total = my_cur.fetchone()[0]
print(f"Total rows in terms: {total:,}")

my_cur.execute("SELECT COUNT(*) FROM terms WHERE stn_id IS NULL")
null_stn = my_cur.fetchone()[0]
print(f"Rows with null stn_id: {null_stn:,}")

my_cur.execute("SELECT COUNT(*) FROM terms WHERE concept_id IS NULL")
null_concept = my_cur.fetchone()[0]
print(f"Rows with null concept_id: {null_concept:,}")

my_cur.execute("SELECT COUNT(DISTINCT tty) FROM terms")
tty_count = my_cur.fetchone()[0]
print(f"Distinct TTY values: {tty_count:,}")

print("\nSample rows:")
my_cur.execute("""
    SELECT id, term, tty, concept_id, semantic_type, source, code, stn_id
    FROM terms
    LIMIT 5
""")
for row in my_cur.fetchall():
    print(row)

# ─────────────────────────────────────────
# CLEANUP
# ─────────────────────────────────────────
pg_cur.close()
pg_conn.close()
my_cur.close()
my_conn.close()

print("\n✅ terms table population complete!")