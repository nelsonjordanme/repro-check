# Demo assets

A terminal cast of a first-run of `repro-check` on the bundled broken fixture
(`fixtures/example_paper` — a hardcoded absolute data path + a removed
`np.float` API). The cast is **synthesized from the tool's real captured
output** (the exact before/after text a live run produces), not screen-recorded
live — so the timing is illustrative, but every line shown is real tool output.

- **`quickstart.cast`** — [asciicast v2](https://docs.asciinema.org/manual/asciicast/v2/).
  Play it in a terminal with:

  ```bash
  pipx run asciinema play demo/quickstart.cast   # or: asciinema play demo/quickstart.cast
  ```

- **`quickstart.txt`** — a plain-text transcript of the same run, for a quick
  glance without a player.

## Making an animated GIF (optional)

To embed a GIF in the README, convert the cast with
[agg](https://github.com/asciinema/agg):

```bash
agg demo/quickstart.cast demo/quickstart.gif
```

then reference `demo/quickstart.gif` from the top-level README. (The `.cast` is
the source of truth; the GIF is a derived artifact and is intentionally not
committed.)
