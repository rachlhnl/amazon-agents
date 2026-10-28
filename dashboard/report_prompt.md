Write this week's Dagacci KPI report into the dashboard's shared database. Only add a report document; never change or delete any other document.

Artifact: https://claude.ai/artifact/EQVvremS6huZYvNAY1BswG (use the ArtifactData tool with this url)
KPI data: ~/Documents/dagacci/dashboard/kpis.json and ~/Documents/dagacci/dashboard/history.json

1. Read kpis.json. The data week is customers.latest.week_end (call it W). The report id is `week-W`.
2. List the `reports` collection. If `week-W` already exists, stop: the team or an earlier run already wrote it.
3. Activity window: `from` = the largest `to` among existing `week-*` reports, or 7 days ago if none; `to` = now. Both are ISO timestamps (UTC, e.g. 2026-10-27T07:00:00.000Z).
4. List the `actions` and `log` collections. From `log`, collect entries with `at` inside the window: actions whose `changes.status[1]` is "done" (use `state.title`, `state.team` and `state.doneNote`), and new team to-dos (`changes.custom`). From `actions`, note anything with status "blocked", or with a `due` date before today and a status other than done or dropped.
5. Write 3 to 5 summary bullets for a CEO-led weekly review, using only these facts and kpis.json (`scorecard`, `changes`, `insights`). Lead with the biggest KPI move against target, then what the team completed (quote its done note), then what is new, blocked or past due and needs a decision. Each bullet is one plain sentence under 30 words, with no markdown and no invented numbers.
6. Create the document with ArtifactData `set`: collection `reports`, doc_id `week-W`, data
   {"kind": "week", "key": W, "summary": [...], "generatedAt": <now ISO>, "generatedBy": "auto", "from": <from>, "to": <to>}.
7. If today is in the first 7 days of a month and history.json has weeks ending in last month, do the same for `month-YYYY-MM` (last month), unless it already exists. Leave out `from` and `to`; the activity window is the calendar month. Use the weeks of that month in history.json and the log entries dated in that month.

Reply with the report ids you wrote, or "nothing to write".
