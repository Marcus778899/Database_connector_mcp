## Scanning is a background job

`inventory_start` returns a job id and comes back immediately; the scan itself
runs on. Poll `inventory_status` rather than assuming, and do not start a second
scan because the first seems slow — an unfinished run resumes from its cursor,
and containers that have not changed are skipped, so a restart is usually a
longer route to the same place.

Once it has finished, read the stored catalog through the `inventory_*` tools
above. Those answer from what the scan recorded and never touch the source.
