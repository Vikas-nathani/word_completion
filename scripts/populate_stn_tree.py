import os
import psycopg2
import mysql.connector

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

# ─────────────────────────────────────────
# STEP 1 — Connect to PostgreSQL
# ─────────────────────────────────────────
print("Connecting to PostgreSQL...")
pg_conn = psycopg2.connect(**PG_CONFIG)
pg_cur = pg_conn.cursor()
print("Connected to PostgreSQL ✅")

# ─────────────────────────────────────────
# STEP 2 — Fetch STN + semantic info from mrsty
# ─────────────────────────────────────────
print("\nFetching STN paths from mrsty...")
pg_cur.execute("""
    SELECT DISTINCT ON (stn) stn, tui, sty
    FROM mrsty
    WHERE stn IS NOT NULL
    ORDER BY stn, tui
""")
mrsty_rows = pg_cur.fetchall()
print(f"Fetched {len(mrsty_rows)} distinct STN paths ✅")

# ─────────────────────────────────────────
# STEP 3 — Fetch term_count per STN from autocomplete_terms
# (single scan — fast!)
# ─────────────────────────────────────────
print("\nCounting terms per STN from autocomplete_terms...")
pg_cur.execute("""
    SELECT stn, COUNT(*) as term_count
    FROM autocomplete_terms
    WHERE stn IS NOT NULL
    GROUP BY stn
""")
term_count_map = {row[0]: row[1] for row in pg_cur.fetchall()}
print(f"Term counts fetched for {len(term_count_map)} STN paths ✅")

# ─────────────────────────────────────────
# STEP 4 — Derive hierarchy fields for each STN path
# ─────────────────────────────────────────
print("\nDeriving hierarchy fields...")

def get_parent_stn(stn_path):
    if "." in stn_path:
        # A1.1 → A1, A1.1.1 → A1.1
        return ".".join(stn_path.split(".")[:-1])
    elif len(stn_path) > 1:
        # A1 → A, B1 → B, B2 → B
        return stn_path[0]
    else:
        # A, B → true roots
        return None

def get_ancestor_path(stn_path):
    # Build full chain including root
    if "." in stn_path:
        parts = stn_path.split(".")
        # First part is like A1, B1 etc
        first = parts[0]
        root = first[0]  # A or B
        ancestors = [root, first]
        for i in range(1, len(parts)):
            ancestors.append(".".join(parts[:i+1]))
    elif len(stn_path) > 1:
        # e.g A1 → [A, A1]
        ancestors = [stn_path[0], stn_path]
    else:
        # e.g A → [A]
        ancestors = [stn_path]
    return "/".join(ancestors)

def get_depth_level(stn_path):
    if "." in stn_path:
        # A1.1 → 3, A1.1.1 → 4
        return len(stn_path.split(".")) + 1
    elif len(stn_path) > 1:
        # A1, B1 → depth 2
        return 2
    else:
        # A, B → depth 1
        return 1

# Collect all stn_paths to determine is_leaf
all_stn_paths = set(row[0] for row in mrsty_rows)

def is_leaf_node(stn_path):
    for path in all_stn_paths:
        if path != stn_path:
            # Check both A. pattern and direct prefix like A→A1
            if path.startswith(stn_path + ".") or (
                len(stn_path) == 1 and path.startswith(stn_path) and len(path) > 1
            ):
                return False
    return True

# Build rows to insert
rows_to_insert = []
for stn_path, tui, sty in mrsty_rows:
    parent_stn    = get_parent_stn(stn_path)
    ancestor_path = get_ancestor_path(stn_path)
    depth_level   = get_depth_level(stn_path)
    is_leaf       = is_leaf_node(stn_path)
    term_count    = term_count_map.get(stn_path, 0)

    rows_to_insert.append((
        stn_path,
        parent_stn,
        ancestor_path,
        depth_level,
        sty,
        tui,
        is_leaf,
        term_count
    ))

print(f"Derived hierarchy for {len(rows_to_insert)} rows ✅")

# ─────────────────────────────────────────
# STEP 5 — Connect to MySQL and insert
# ─────────────────────────────────────────
print("\nConnecting to MySQL...")
my_conn = mysql.connector.connect(**MYSQL_CONFIG)
my_cur = my_conn.cursor()
print("Connected to MySQL ✅")

print("\nInserting rows into stn_tree...")
insert_sql = """
    INSERT INTO stn_tree (
        stn_path, parent_stn, ancestor_path, depth_level,
        semantic_type_name, semantic_type_id, is_leaf, term_count
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""
my_cur.executemany(insert_sql, rows_to_insert)
my_conn.commit()
print(f"Inserted {my_cur.rowcount} rows into stn_tree ✅")

# ─────────────────────────────────────────
# STEP 6 — Verify
# ─────────────────────────────────────────
print("\nVerifying...")
my_cur.execute("SELECT COUNT(*) FROM stn_tree")
total = my_cur.fetchone()[0]
print(f"Total rows in stn_tree: {total}")

my_cur.execute("SELECT COUNT(*) FROM stn_tree WHERE is_leaf = TRUE")
leaf_count = my_cur.fetchone()[0]
print(f"Leaf nodes: {leaf_count}")

my_cur.execute("SELECT COUNT(*) FROM stn_tree WHERE parent_stn IS NULL")
root_count = my_cur.fetchone()[0]
print(f"Root nodes: {root_count}")

print("\nSample rows:")
my_cur.execute("SELECT * FROM stn_tree ORDER BY stn_path LIMIT 5")
for row in my_cur.fetchall():
    print(row)

# ─────────────────────────────────────────
# CLEANUP
# ─────────────────────────────────────────
pg_cur.close()
pg_conn.close()
my_cur.close()
my_conn.close()

print("\n✅ stn_tree population complete!")