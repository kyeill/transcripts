# transcripts

Weekly speaker-labeled transcripts of Christ Church (christchurchgr.org) Sunday services.

## How it works

1. **Find this week's service** — `christchurchgr.org/sermons` is a client-rendered
   Next.js app; there are no real links in the static HTML, so `fetch_episode.py`
   drives a headless browser (Playwright) to read the rendered DOM: the first
   `/episode/<slug>` link on the sermons page is the latest service. From that
   episode page it pulls the "Listen to the entire service here" audio URL and
   the "Worship Guide" PDF URL.
2. **Parse the worship guide** (`worship_guide.py`) — the PDF is real text (not
   scanned) and lists the entire order of service in sequence: every song title,
   prayer, reading, and who leads each spoken part (pastor, elder, missionary
   guest, etc). This is the ground truth for who's speaking and when something
   is music rather than speech.
3. **Transcribe** (`transcribe.py`) — sends the full-service audio to AssemblyAI
   (cheapest available diarization-capable API) and gets back a transcript with
   timestamps.
4. **Align** (`align.py`) — walks the transcript in order and matches it against
   the worship guide sequence: known song lyrics (given in full in the PDF for
   hymns/offertory) get tagged as music and excluded from the spoken transcript;
   everything else gets labeled with the name from the worship guide.
5. **Render** (`render.py`) — writes a formatted .docx with speaker-labeled
   sections.

## Running

Requires `ASSEMBLYAI_API_KEY` (see below).

```bash
python main.py                     # latest episode
python main.py --url <episode-url> # a specific episode
```

## Automation

`.github/workflows/weekly.yml` runs every 4 hours. Each run checks whether the
current ISO week already has a committed transcript; if so it's a no-op. This
means the first successful run after Sunday's service (normally the Monday
6am slot) does the work, and any run that fails (service not posted yet, API
hiccup, etc) is retried automatically every 4 hours until one succeeds —
without ever producing more than one transcript per week.

### Secrets

- `ASSEMBLYAI_API_KEY` — repo secret, get a key at assemblyai.com.
