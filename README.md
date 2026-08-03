# pinch-hit-model

Two MLB betting bots that flag players likely to be pinch-hit / lifted early, for **under** bets (H+R+RBI, hits).

| File | What it is |
|---|---|
| `pinch_hit_pregame_v1.py` | **Pregame pull-risk board.** When MLB posts a game's official lineup, it ranks the starters by how likely each is to be lifted early and posts a Discord card. Grades itself nightly and stores results in SQLite. |
| `pinch_hit_bot_v17_9.py` | **Live in-game alert bot.** Streams beat-reporter tweets and pings Discord the moment a pinch-hit is announced mid-game. |

---

## Pregame board

### How it works
1. Polls the schedule; when a game's **official batting order** is posted, it analyzes both lineups.
2. Scores each starter 0–100 on pull risk from: platoon split vs the starter, opposing **bullpen** handedness, recent form, batter-vs-pitcher history, a better **bench** bat, **manager** pinch-hit tendency (last 2 weeks), lineup spot, and everyday-vs-part-time role.
3. Posts a Discord embed **only if** a game's top pick clears `POST_MIN_SCORE` (so you only get pinged on real spots).
4. At ~3am ET it grades the prior day vs actual box scores and writes everything to `pregame_results.db`.

### Run modes
```bash
python pinch_hit_pregame_v1.py --serve          # operational: watch lineups, post per game, grade nightly
python pinch_hit_pregame_v1.py --print          # print the whole-slate board to console (testing)
python pinch_hit_pregame_v1.py --results [DATE]  # grade a day's picks vs results (default: yesterday)
python pinch_hit_pregame_v1.py --stats          # hit-rate breakdowns from the DB (by scenario, confidence band)
```

### Environment variables
| Var | Required? | Default | Purpose |
|---|---|---|---|
| `PREGAME_WEBHOOK_URL` | **Yes** | — | Discord webhook the board posts to |
| `DATA_DIR` | **Yes on Railway** | (cwd) | Directory for all state (DB, predictions, caches). Point at a volume. |
| `ANTHROPIC_API_KEY` | No | — | Adds a one-line plain-English summary per pick |
| `POST_MIN_SCORE` | No | `60` | Only ping a game if its top pick ≥ this (lower = more volume) |
| `MIN_SCORE` | No | `55` | Hide individual picks below this confidence |
| `GAME_TOP_N` | No | `5` | Max picks shown per game |
| `MANAGER_LOOKBACK_DAYS` | No | `14` | Window for coach pinch-hit tendency (0 = off) |
| `RECENCY_DAYS` | No | `14` | Window for the recent-form signal |
| `POLL_MINUTES` | No | `10` | How often serve mode checks for new lineups |
| `RESULTS_HOUR_ET` | No | `3` | Hour (ET) to grade the prior day |
| `SEASON` | No | current year | Season for stats splits |

### The results database (`pregame_results.db`)
- **`picks`** — every posted pick with its prediction (score, scenario, reasons) **and** actual outcome (PA/H/R/RBI/pulled/hit). This is what makes "why are we losing?" queryable.
- **`manager_tendency`** — daily snapshot of each coach's last-2-weeks pinch-hit rate/tier.

`--stats` prints the summary; open the `.db` in any SQLite tool for custom queries.

---

## Deploy on Railway
1. Point a Railway service at this repo. The `Procfile` runs the pregame board in `--serve`.
2. **Add a Volume** to the service and mount it (e.g. `/data`).
3. Set variables: `DATA_DIR=/data`, `PREGAME_WEBHOOK_URL=...` (and optionally `ANTHROPIC_API_KEY`).
4. Redeploy.

Without the volume + `DATA_DIR`, Railway's ephemeral filesystem wipes the database and accuracy history on every redeploy.

To run the **live bot** instead, use a separate service with start command `python pinch_hit_bot_v17_9.py` and its own variables (`TWITTER_BEARER_TOKEN`, `PINCH_HIT_WEBHOOK_URL`, `DISCORD_CHANNEL_ID`, `ANTHROPIC_API_KEY`, …).
