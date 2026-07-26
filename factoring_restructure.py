"""
Restructure factoring companies into the proper brand-company + per-address-location
model (project 2's migration 004 design).

For each factoring BRAND (grouped by canonical name):
  • keep one company (the brand), rename it to the canonical brand name;
  • turn every original address-variant into a `company_location` under that brand,
    carrying the original name/address (banking filled later from void-cheque emails);
  • repoint each carrier_invoice to (brand company, its location);
  • delete the now-redundant duplicate brand company rows.

This PRESERVES each distinct remittance address (as a location with its own banking)
— it is not the lossy name-merge that was correctly rejected earlier.

    python factoring_restructure.py            # dry-run: show the plan, no writes
    python factoring_restructure.py --commit   # apply, in one transaction
"""
import argparse
import os
import sys
from collections import defaultdict

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
from dotenv import load_dotenv

load_dotenv(os.path.join(REPO, ".env"))

from db import DATABASE_URL, USE_DB  # noqa: E402
from factoring_names import canonical_factoring_name  # noqa: E402


def _load(conn):
    with conn.cursor() as cur:
        cur.execute("""
            select c.company_id, c.name, count(ci.carrier_invoice_id) as invs
            from companies c
            left join carrier_invoices ci on ci.factoring_company_id = c.company_id
            where c.type = 'factoring'
            group by c.company_id, c.name
            order by c.company_id""")
        rows = cur.fetchall()
    groups = defaultdict(list)
    for cid, name, invs in rows:
        groups[canonical_factoring_name(name)].append((cid, name, invs))
    return groups


def dry_run():
    import psycopg
    conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=15)
    groups = _load(conn)
    conn.close()
    companies_before = sum(len(v) for v in groups.values())
    locations = companies_before  # one location per original address
    deletes = companies_before - len(groups)
    print(f"\n{'='*66}\n  FACTORING RESTRUCTURE — dry run\n{'='*66}")
    print(f"{companies_before} factoring companies → {len(groups)} brand companies "
          f"+ {locations} locations  (delete {deletes} redundant rows)\n")
    for canon, members in sorted(groups.items(), key=lambda kv: -sum(m[2] for m in kv[1])):
        if len(members) == 1:
            continue
        keeper = min(members, key=lambda m: m[0])[0]
        total = sum(m[2] for m in members)
        print(f"  ▸ '{canon}'  (brand = company #{keeper}, {len(members)} locations, {total} invoices)")
        for cid, name, invs in sorted(members, key=lambda m: m[0]):
            print(f"        location ← #{cid:<4} {invs:>3} inv  {name!r}")
    singles = sum(1 for v in groups.values() if len(v) == 1)
    print(f"\n  + {singles} single-location brands (1 company, 1 location each, just cleaned name)")
    print(f"{'='*66}\n")


def print_sql():
    """Emit the full transactional SQL for the approved grouping (for MCP execute)."""
    import psycopg
    conn = psycopg.connect(DATABASE_URL, autocommit=True, connect_timeout=15)
    groups = _load(conn)
    conn.close()
    q = lambda s: s.replace("'", "''")  # noqa: E731
    vals = []
    for canon, members in groups.items():
        keeper = min(m[0] for m in members)
        for cid, _, _ in members:
            vals.append(f"({cid},'{q(canon)}',{keeper})")
    sql = f"""begin;
create temporary table _fmap (old_id int, canonical text, keeper_id int) on commit drop;
insert into _fmap (old_id, canonical, keeper_id) values
{",".join(vals)};
create temporary table _locmap (old_id int, location_id int) on commit drop;
do $$
declare r record; lid int;
begin
  for r in select f.old_id, f.keeper_id, c.name as nm from _fmap f join companies c on c.company_id=f.old_id loop
    insert into company_locations (company_id, location_name, payment_notes)
      values (r.keeper_id, left(r.nm,200), 'imported from sheet; banking pending')
      returning location_id into lid;
    insert into _locmap (old_id, location_id) values (r.old_id, lid);
  end loop;
end $$;
update carrier_invoices ci set factoring_company_id=f.keeper_id, factoring_location_id=lm.location_id
  from _fmap f join _locmap lm on lm.old_id=f.old_id where ci.factoring_company_id=f.old_id;
update companies c set default_factoring_company_id=f.keeper_id
  from _fmap f where c.default_factoring_company_id=f.old_id and f.old_id<>f.keeper_id;
update companies c set name=f.canonical
  from (select distinct keeper_id, canonical from _fmap) f
  where c.company_id=f.keeper_id and c.name<>f.canonical;
delete from companies where company_id in (select old_id from _fmap where old_id<>keeper_id);
commit;"""
    print(sql)


def commit():
    import psycopg
    conn = psycopg.connect(DATABASE_URL, autocommit=False, connect_timeout=15)
    made_locations = repointed = deleted = renamed = 0
    try:
        groups = _load(conn)
        with conn.cursor() as cur:
            for canon, members in groups.items():
                keeper = min(m[0] for m in members)
                loc_for = {}
                for cid, name, _ in members:
                    cur.execute(
                        """insert into company_locations (company_id, location_name, payment_notes)
                           values (%s, %s, %s) returning location_id""",
                        (keeper, name[:200], "imported from sheet; banking pending"))
                    loc_for[cid] = cur.fetchone()[0]
                    made_locations += 1
                for cid, _, _ in members:
                    cur.execute(
                        """update carrier_invoices
                           set factoring_company_id=%s, factoring_location_id=%s
                           where factoring_company_id=%s""",
                        (keeper, loc_for[cid], cid))
                    repointed += cur.rowcount
                    cur.execute("update companies set default_factoring_company_id=%s where default_factoring_company_id=%s",
                                (keeper, cid))
                cur.execute("update companies set name=%s where company_id=%s and name<>%s",
                            (canon, keeper, canon))
                renamed += cur.rowcount
                others = [m[0] for m in members if m[0] != keeper]
                if others:
                    cur.execute("delete from companies where company_id = any(%s)", (others,))
                    deleted += len(others)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    print(f"created {made_locations} locations, repointed {repointed} invoices, "
          f"renamed {renamed} brands, deleted {deleted} redundant company rows")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--print-sql", action="store_true")
    args = ap.parse_args()
    if not USE_DB:
        raise SystemExit("DATABASE_URL not set")
    if args.print_sql:
        print_sql()
    elif args.commit:
        commit()
    else:
        dry_run()
