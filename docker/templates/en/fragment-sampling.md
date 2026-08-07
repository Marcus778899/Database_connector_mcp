## Rows you sample are masked

`get_sample` is the one tool that returns real data, so it masks personal
columns by default: `a***@***.com`, `***`. The shape survives, which is what
reasoning about a column needs; the value does not, because a sampled row goes
into your context and from there into every transcript and log that context
touches.

Do not ask for `mask=False` out of habit. Ask for it when the task genuinely
turns on the literal value, and expect it to be refused — it is a separate
grant, not a parameter you can talk the server into.
