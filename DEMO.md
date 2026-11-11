# Margin: 75-second demo script

**Setup before recording:** a Google Doc titled "Launch plan" with 3–4 paragraphs, one clumsy
sentence ("Our product is very good and people likes it a lot because it is fast.") and a vague
line ("We will ship sometime later this year."). Hermes gateway running with Margin, the doc
already watched, a terminal tiled next to the browser. Browser zoom 125%.

| Time | On screen | Voice-over |
|---|---|---|
| 0:00–0:07 | Title card: "Margin: Hermes in the margins of your docs" | "Your team already talks in Google Docs comments. Margin puts Hermes right there." |
| 0:07–0:15 | Terminal: `hermes margin watch https://docs.google.com/document/d/…` → "Watching …" | "One command to watch a doc. Sign-in is Hermes's own Google OAuth; Margin never touches a token." |
| 0:15–0:27 | Highlight the clumsy sentence, comment `@hermes make this crisp and grammatical`, post | "Highlight text, mention @hermes, ask for anything." |
| 0:27–0:37 | Reply appears: "🪶 Margin is reading the doc…" → "is thinking…" → becomes the answer with "Proposed replacement: “People love our product because it's fast.” Reply "apply"…" | "Margin posts a status reply and edits it in place. Hermes sees the whole doc and exactly what you highlighted." |
| 0:37–0:47 | Type `apply` in the thread → sentence in the doc changes; Margin replies "Applied." | "Like it? Reply apply. Margin swaps the text, and only when it appears exactly once, so it never edits the wrong spot." |
| 0:47–0:58 | Same thread: "Does that contradict anything else in the doc?" → Hermes answers referencing the ship-date line | "Follow-ups keep the thread's memory: each comment thread is its own Hermes session." |
| 0:58–1:08 | New comment on "We will ship sometime later this year." with `@hermes turn this into a dated milestone list`; answer appears | "Every thread is independent. Works in Slides and Sheets comments too." |
| 1:08–1:15 | README privacy section, then end card: "hermes plugins install pratikpoddar/hermes-margin" | "Doc text goes only to your own model; nothing else leaves Google. Margin. Hermes, in the margins." |

**Tips:** set `MARGIN_POLL_INTERVAL=3` for the recording so replies land quickly; use a fast
model; record at 1080p and trim the waits; export a 10–15 s GIF of 0:15–0:47 as `docs/demo.gif`.
