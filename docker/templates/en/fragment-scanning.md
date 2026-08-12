## Scanning is a background job

`inventory_start` returns a job id and comes back immediately; the scan itself
runs on. Poll `inventory_status` rather than assuming, and do not start a second
scan because the first seems slow — an unfinished run resumes from its cursor,
and containers that have not changed are skipped, so a restart is usually a
longer route to the same place.

Once it has finished, read the stored catalog through the `inventory_*` tools
above. Those answer from what the scan recorded and never touch the source.

**If this is not the first scan of this source, ask `inventory_changes` first.**
It names the tables that appeared or disappeared and the columns that were added
or retyped since last time. With that list you only have to look at what
actually moved instead of reading the database again — a scan leaves descriptions
already written alone.
