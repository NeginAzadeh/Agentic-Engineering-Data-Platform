-- Check the actual adapter connection, rather than only inspecting YAML.
select 'source attachment missing or writable' as issue
where not exists (
    select 1 from duckdb_databases()
    where database_name = 'engineering_source' and readonly
)
