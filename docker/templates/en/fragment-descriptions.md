## Writing descriptions

`inventory_annotate` is how what you work out gets kept.

To describe a whole database, work **a page at a time, not a table at a time**:

1. `inventory_columns(database=…, only_missing_description=True, include_profile=False, limit=200)`
   — one page of columns nobody has described yet, spanning several tables
2. Infer what each one holds from its name, its type, and the other columns of
   the same table
3. `inventory_annotate(database=…, containers=[…])` — write every table on that
   page back in one call
4. Pass the `next_cursor` you were given back into step 1, until there is none

One `inventory_columns` call per table is one round trip per table on a source
with a few hundred of them, and each of those carries statistics you are not
going to use. That is what `include_profile=False` is for.

Two rules:

- A description read from the source is not yours to overwrite; the server keeps
  those in a separate column and a scan refreshes them.
  `only_missing_description=True` has already left them out, so what is on the
  page is genuinely undescribed.
- Your descriptions are recorded as an agent's guesses. Say what you inferred
  them from. If you are unsure, say that in the description rather than leaving
  a confident sentence for someone to trust later.

Report after each page rather than after the whole run. Nothing already written
is lost if you are interrupted, and the next attempt picks up wherever
`only_missing_description` still returns rows.
