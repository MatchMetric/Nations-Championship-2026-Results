# Nations Championship 2026 — results feed

`results.json` is the live results feed for the [Nations Championship 2026 Predictor](https://matchmetric.github.io/Nations-Championship-2026/).

## Format

- **ids 1–36, group fixtures.** `a`/`b` = final score (home team as listed first), `ta`/`tb` = tries (they drive the 4-try bonus point), `ko_utc` = kick-off time in UTC, `report` = optional one-line summary.
- **ids 37–42, Finals Weekend at Twickenham.** `north`/`south` = the teams. `a`/`ta` are the Northern team's score and tries, `b`/`tb` the Southern team's.
- Matches that haven't been played have `null` scores, and the app ignores them.

## Automatic updates

`.github/workflows/update-results.yml` runs `scripts/update_results.py`:

- **Schedule:** hourly on match days (Fri–Sun UTC), plus a daily catch-up. You can also run it by hand from the Actions tab, with an optional dry run.
- **No calls when nothing is due:** if no match kicked off more than 2¼ hours ago without a score, the run ends without calling the API.
- **Looking up results:** otherwise it asks Claude (with web search) for the final score and tries. It makes two independent lookups and only writes a result if:
  - both lookups agree exactly
  - each is backed by at least two websites that really appeared in its searches
  - the score is arithmetically possible for that many tries
- **Never overwrites:** scores already in the file are left alone, so hand corrections always win.
- **Run summary:** each run's summary shows what was filled in, what was skipped and why, and the token and search usage.

**Setup:** add a repository secret named `ANTHROPIC_API_KEY` (Settings → Secrets and variables → Actions). Optional: add a repository *variable* `CLAUDE_MODEL` to change the model (default `claude-sonnet-5-5`).
