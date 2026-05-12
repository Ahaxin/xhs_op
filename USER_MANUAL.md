# XHS Automation User Manual

**Version 1.0** — Complete guide to operating the dual-account content engine for Xiaohongshu (XHS) from your Windows machine.

---

## Table of Contents

1. [Overview](#overview)
2. [System Requirements & Setup](#system-requirements--setup)
3. [Initial Account Setup](#initial-account-setup)
4. [Dashboard Interface](#dashboard-interface)
5. [Creating & Editing Drafts](#creating--editing-drafts)
6. [Publishing Posts](#publishing-posts)
7. [Monitoring Engagement](#monitoring-engagement)
8. [Hot Topic Search](#hot-topic-search)
9. [Copying & Inspired Drafts](#copying--inspired-drafts)
10. [Troubleshooting](#troubleshooting)

---

## Overview

This tool automates content creation and posting for two independent Xiaohongshu accounts:

- **`@banna-villa`** — Xishuangbanna rental properties (villas, apartments, luxury hotels)
- **`@stock-ai-digest`** — AI / crypto / stock market digests and hot takes

### How It Works

```
┌─────────────────────────────────────────────┐
│      Content Sources (X, XHS, RSS)          │
│  ┌─────────────┬──────────────┬─────────┐   │
│  │ X Tweets    │ XHS Posts    │ News    │   │
│  │ (twikit)    │ (MediaCrawler)│ Feeds   │   │
│  └──────┬──────┴──────┬───────┴────┬────┘   │
└─────────┼─────────────┼────────────┼────────┘
          │             │            │
          ▼             ▼            ▼
    ┌─────────────────────────────────┐
    │   AI Drafting Engine (Claude)   │
    │   + Image Generation (Gemini)   │
    └────────────────┬────────────────┘
                     ▼
        ┌────────────────────────┐
        │  Review Dashboard (you)│
        │  Edit • Approve • Post │
        └────────┬───────────────┘
                 ▼
        ┌────────────────────────┐
        │   Scheduler (every 5m) │
        │   APScheduler daemon   │
        └────────┬───────────────┘
                 ▼
           ┌──────────────┐
           │  XHS Live    │
           │  (posted)    │
           └──────┬───────┘
                  │
                  ▼
        ┌────────────────────────┐
        │  Engagement Monitor    │
        │  (poll comments every  │
        │   15 min, draft replies)
        └────────────────────────┘
```

**Your workflow:** 10 minutes per day (morning + evening) to review, approve, and publish drafts. Everything else is automated.

---

## System Requirements & Setup

### Prerequisites

- **Windows 11** (tested on Windows 11 Home China)
- **Python 3.13** installed via uv (`uv` is the package manager — see below)
- **Residential proxy** for XHS (banna-villa account must appear from CN Yunnan region)
- **API keys** for Claude, OpenAI (GPT-5), Gemini, Qwen (DashScope)
- **LM Studio** running locally (optional, for bulk/variant generations)
- **~1GB disk space** for SQLite DB + image assets

### Installing uv (Package Manager)

1. Download uv from https://github.com/astral-sh/uv/releases — choose `uv-x86_64-pc-windows-msvc.exe`
2. Run the installer and add uv to PATH (check "Add uv to PATH" during install)
3. Verify: open PowerShell and run `uv --version` — should print version number

### Cloning & Installing XHS Automation

```powershell
# Clone the repo
git clone https://github.com/yourusername/xhs_op.git
cd xhs_op

# Install dependencies (uv handles everything)
uv sync
```

### Environment Configuration

1. **Copy the example .env file:**
   ```powershell
   Copy-Item .env.example -Destination .env
   ```

2. **Edit `.env` with your API keys & proxy URLs:**
   ```ini
   # Claude (Anthropic)
   ANTHROPIC_API_KEY=sk-ant-...

   # OpenAI (GPT-5)
   OPENAI_API_KEY=sk-...

   # Gemini (for image generation)
   GEMINI_API_KEY=AIzaSy...

   # Qwen (DashScope, for villa personas)
   DASHSCOPE_API_KEY=sk-...

   # Residential proxies
   BANNA_PROXY_URL=socks5://user:pass@proxy-yunnan.922s5.com:10810
   STOCK_PROXY_URL=socks5://user:pass@proxy-generic.922s5.com:10810

   # LM Studio (if you run it locally)
   LMSTUDIO_BASE_URL=http://localhost:1234/v1

   # X / Twitter credentials (for scraping)
   X_USERNAME=your_burner_account
   X_PASSWORD=password
   X_EMAIL=burner@gmail.com
   ```

   **Note:** Never commit `.env` — it's git-ignored.

3. **Verify setup:**
   ```powershell
   uv run python -c "from xhs_op.db import init_db; init_db()"
   ```
   If successful, `data/xhs_op.db` is created.

---

## Initial Account Setup

### Step 1: Create XHS Accounts

**For `@banna-villa` (property rentals):**
1. Create a new XHS account from a **real Chinese phone number** (or virtual SIM).
2. Use the Yunnan residential proxy (IP must geolocate to Yunnan).
3. Manual warmup: log in daily for 7 days, post nothing, engage with 5–10 posts per day (like, follow, browse).
4. After 7 days, the account is ready for automation.

**For `@stock-ai-digest` (stock/crypto content):**
1. Create a new XHS account (can use any region proxy).
2. Same 7-day manual warmup: daily logins, light engagement, no posts.

### Step 2: Collect Cookies

Once each account is warmed up:

```powershell
# For banna account
uv run python scripts/login.py --account banna

# For stock account
uv run python scripts/login.py --account stock
```

Each script:
1. Opens a **non-headless Chromium browser** (you see it on screen)
2. Navigates to XHS login page
3. **Scan the QR code with your phone**
4. Once logged in, the browser auto-captures cookies → saves to `data/cookies/banna.json` or `data/cookies/stock.json`

**Keep these cookie files fresh** — they expire every ~7 days. Re-run the login script when you see "cookie expired" errors.

### Step 3: Configure Proxy & Settings

Edit `src/xhs_op/config.py` if you need to change:
- Proxy URLs
- Model routing (which LLM for which persona)
- Timezone (default: Asia/Shanghai)

**Default routing:**
- `villa` persona → Claude Sonnet 4.6 (hook) + Qwen3-Max (body)
- `stock_digest` → GPT-5
- `stock_hottake` → Claude Sonnet 4.6
- Fallback bulk → LM Studio local

---

## Dashboard Interface

### Starting the Dashboard

```powershell
uv run streamlit run src/xhs_op/dashboard/app.py
```

Opens at `http://localhost:8501` in your default browser.

### Dashboard Tabs

#### 1. **Queue** — Pending Review Drafts

Shows all drafts waiting for your approval. For each draft:

**Top info bar:**
- Account (banna / stock)
- Persona (villa / stock_digest / stock_hottake)
- Status (pending_review / scheduled / published / discarded)
- Inspiration note (where it came from)

**Editable fields:**
- **Title** (≤20 chars) — required for XHS, must be catchy
- **Body** (≤1000 chars) — main content
- **Hashtags** — auto-filled, editable (e.g., `#版纳民宿`, `#股票`)

**Image gallery:**
- Thumbnails of all attached images
- **🔄 Regenerate image** button — triggers AI image gen for that slot
- Drag to reorder images (left-to-right is XHS display order)

**Action buttons:**
- **✅ Approve** — moves to "Schedule" tab; you pick publish time
- **📝 Edit** — inline edit title/body/hashtags, then save
- **❌ Discard** — deletes draft (cannot undo)
- **🔄 Regenerate** — re-run LLM to draft new title/body (keeps same image)

---

#### 2. **Schedule** — Approved Drafts Awaiting Publishing

Calendar view of drafts scheduled to publish. For each draft:

**Visible info:**
- Publish time (e.g., "May 12, 10:30 AM")
- Account
- Title preview
- Current status

**Actions:**
- **Drag to reschedule** — move on calendar to new date/time
- **Cadence check** — green checkmark if 90-min gap is respected between same-account posts
- **View details** — expand to see full title/body before publishing
- **Unschedule** — move back to Queue for re-review

**Important:** The scheduler runs every 5 minutes and publishes any draft whose scheduled time has passed (within ±15 min jitter window).

---

#### 3. **Inspiration** — Hot Topics & Competitor Posts

Read-only feed of trending XHS posts from your watchlist keywords:
- `西双版纳民宿`, `奢华酒店`, `AI热点`, `股票分析`, etc.

For each inspiration post:

**Display:**
- Thumbnail + author
- Title + body
- Engagement (likes, comments, saves)

**Action:**
- **✨ Generate Inspired Draft** button → triggers the inspector, creates a new draft in Queue based on this post's structure (not plagiarism — legal reinterpretation)

**How inspired drafts work:**
1. Extract pattern: hook style, body structure, hashtag cluster, image layout
2. Re-instantiate with your own content (villa photo or stock analysis)
3. Plagiarism guard: reject if >15% 5-gram overlap with original
4. Land in Queue for your review

---

#### 4. **Comments** — Engagement & Replies

All incoming comments on your posts, organized by account.

**For each comment:**
- Author, comment text
- Intent classification:
  - `question` — someone asking about rentals / market
  - `compliment` — positive feedback
  - `rental_intent` — "how to book?" / "price?"
  - `report_intent` — feedback / corrections on analysis
  - `spam` — ignore

**Status:**
- `pending_approval` (default) — AI drafted a reply, waiting for you
- `approved` — you reviewed it; ready to post
- `published` — reply has been sent to XHS
- `skipped` — you opted not to reply

**For each comment, the system auto-drafts a reply:**
- `question` → helpful 1-2 sentence answer
- `compliment` → warm thanks, on-brand response
- `rental_intent` → "私信我" CTA (DM me), **no prices ever** in feed
- `report_intent` (stock account) → include disclaimer, offer to DM for details
- `spam` → flagged but no draft (save your energy)

**Actions per comment:**
- **✏️ Edit** — modify the drafted reply before posting
- **✅ Approve** → reply gets posted automatically
- **❌ Skip** → don't reply to this comment
- **🔄 Regenerate** — AI drafts a new reply

---

#### 5. **Analytics** — Post Performance

Graph and stats for each published post:

- **Engagement over time** — likes, comments, saves, shares (6h / 24h / 72h checkpoints)
- **Account comparison** — banna vs stock: reach, engagement rate, best-performing post type
- **Content type breakdown** — villa vs stock_digest vs stock_hottake: which performs best

Used for tuning personas and scheduling (post types that underperform may need tweaking).

---

## Creating & Editing Drafts

### Automatic Draft Generation

Drafts are auto-generated from content sources:

1. **X → XHS Translator** (stock account)
   - Monitors 13 AI/crypto/stock X accounts + trending search terms
   - Fetches trending tweets every 30 min
   - LLM translates & adapts for Chinese audience
   - Lands in Queue as `stock_digest` or `stock_hottake` personas

2. **XHS Competitor Tracker** (banna account)
   - Monitors top-performing posts under `西双版纳民宿`, `奢华酒店`, etc.
   - Extracts structural patterns (hook, body shape, hashtags)
   - Re-instantiates with your villa content (own photos, own words)
   - Plagiarism guard: rejects if >15% overlap
   - Lands in Queue as `villa` persona

3. **Manual Drafting**
   - In the **Queue** tab, select "Create Manual Draft" button
   - Fill in account, persona, title, body, upload images
   - Submit → lands in Queue for review

### Editing a Draft

In the **Queue** tab:

1. Find the draft you want to edit
2. Click **📝 Edit**
3. Modify:
   - **Title** — keep under 20 chars (XHS limit)
   - **Body** — under 1000 chars
   - **Hashtags** — comma-separated or one per line
   - **Images** — drag to reorder, click trash to remove, click + to add new
4. Click **Save**
5. Draft is updated; still in Queue for approval

### Image Management

**Adding images to a draft:**
1. In edit mode, click **+ Add image**
2. Pick:
   - **Upload local file** — drag a JPG/PNG from your disk
   - **Generate AI image** — describe what you want (e.g., "rainforest villa at sunset, cinematic lighting")
3. LLM generates image via Gemini 2.5 Flash (Nano Banana) or OpenAI fallback
4. Image appears in gallery; you can reorder or delete

**Image sources:**
- `data/assets/villa_photos/` — your real property photos (banna account)
- `data/assets/generated/` — AI-generated lifestyle images

**For best results:**
- Real photos: sharp, natural lighting, 9:16 ratio (XHS native)
- AI images: descriptive prompts (style, lighting, mood), 9:16 ratio

---

## Publishing Posts

### The Publishing Workflow

1. **Draft is created** → lands in Queue (status: `pending_review`)
2. **You review** → title, body, images, inspiration note
3. **You approve** → draft moves to Schedule (status: `scheduled`)
4. **You set publish time** → via drag-on-calendar or time picker
5. **Scheduler runs every 5 min** → checks if `suggested_publish_at <= now + jitter`
6. **Cadence check:** ≥90 min since last post on same account? → if yes, publish
7. **Publisher posts to XHS** → note id captured, status: `published`
8. **Post monitored** → engagement scraped at 6h / 24h / 72h checkpoints

### Cadence Rules (Anti-Ban)

These are enforced by the scheduler — you don't manually control them, but understand the limits:

- **Gap between posts:** minimum 90 minutes between any two posts on the same account
- **Daily quota (first month warmup):** max 1 post per calendar day per account
- **Jitter window:** publish time = scheduled time ± 15 minutes (random)
- **No headless mode:** when Playwright fallback is used (on signature error), browser is visible (you see it login)

Example schedule:
- 10:00 AM — post `@banna-villa` (approved yesterday evening, scheduled for morning)
- 1:30 PM — post `@stock-ai-digest` (no conflict with banna because different account)
- 3:15 PM — `@banna-villa` again — NO, only 5h 15m since 10 AM, violates 90-min rule
- 11:30 AM (next day) — OK, 25h 30m gap

### What Happens on Publish Error

If the XHS API returns a signature error (`x-s` mismatch):

1. Scheduler catches the error
2. Falls back to **Playwright fallback** — opens a real Chromium browser (non-headless, you see it)
3. Browser auto-fills the form and clicks publish (human-typing simulation)
4. If that also fails, error is logged; you must manually fix or contact support

If the Playwright fallback succeeds, the note id might look like `pw-success-<timestamp>` instead of a native XHS id — this is normal, it's a valid synthetic id.

---

## Monitoring Engagement

### Comment Notifications

Every 15 minutes, the system polls XHS for new comments on your posts:

1. **Fetches notifications** → mentions, likes, follows
2. **Filters to comments only** — ignores pure likes/follows
3. **Classifies intent** → question / compliment / rental_intent / report_intent / spam
4. **Auto-drafts reply** → LLM creates a context-appropriate response
5. **Lands in Comments tab** → status: `pending_approval`

**Your action:** Review replies in the **Comments** tab, approve or edit, then post.

### Metrics Tracking

Engagement is scraped 3 times per post:
- **6 hours after publish** — early traction (likes, saves, comments)
- **24 hours after publish** — day-1 performance
- **72 hours after publish** — sustained engagement

Visible in the **Analytics** tab as engagement curves per post.

---

## Hot Topic Search

### Searching X (Twitter) for Trends

The system auto-monitors X via `data/x_watchlist.yaml`:

```yaml
accounts:
  - handle: @elonmusk
  - handle: @OpenAI
  - handle: @a16z

search_terms:
  - "AI regulation"
  - "crypto bull run"
  - "stock market crash"
```

**How it works:**
1. Every 30 min, the system fetches latest tweets from watchlist accounts + trending search terms
2. Scores by engagement (likes + retweets, normalized 0–100)
3. Stores in `ideas` table as `source='x'`
4. Translator LLM picks high-engagement tweets, adapts for Chinese audience
5. Drafts land in Queue

**To customize watchlist:**
1. Edit `data/x_watchlist.yaml`
2. Add/remove accounts or search terms
3. Save; the feeder picks up changes on next 30-min poll cycle

---

### Searching XHS for Competitor Insights

XHS competitor search is configured in `data/competitor_watchlist.yaml`:

```yaml
banna_villa:
  keywords:
    - "西双版纳民宿"
    - "版纳别墅"
    - "告庄民宿"
  creators:
    - "banna_villa_account_1"
    - "banna_villa_account_2"

luxury_hotel:
  keywords:
    - "奢华酒店"
    - "五星度假"
    - "安缦"
```

**How it works:**
1. Every 30 min, the system scrapes top 50 posts per keyword (requires MediaCrawler in `external/MediaCrawler/`)
2. Extracts structural patterns: hook, body shape, hashtag cluster, image layout
3. Stores in `ideas` table as `source='xhs_competitor'`
4. Inspector LLM re-instantiates patterns with your villa content
5. Plagiarism guard rejects if >15% overlap
6. Drafts land in Queue as `villa` persona

**To update watchlist:**
1. Edit `data/competitor_watchlist.yaml`
2. Add/remove keywords or creator handles
3. Save; feeder picks up on next cycle

---

## Copying & Inspired Drafts

### How "Inspired" Works (Legal, Not Plagiarism)

The system does NOT copy posts verbatim. Instead:

1. **Analyze structure** — extract:
   - Hook pattern (rhetorical question? shocking stat? call-to-action?)
   - Body shape (3 bullets? narrative flow? conclusion?)
   - Hashtag cluster (e.g., 5–8 specific tags)
   - Image layout (single hero image? carousel? text overlay?)

2. **Re-instantiate with your content:**
   - Hook → similar style, your angle (e.g., "Did you know about THIS villa feature?")
   - Body → your own words, your own facts, your own voice
   - Images → real photos of your property (not competitor's)
   - Hashtags → similar cluster, your own tags

3. **Plagiarism guard** — compute 5-gram Jaccard similarity:
   - If `overlap < 15%` → draft succeeds
   - If `overlap >= 15%` → reject, re-draft with stronger paraphrase instruction
   - If still fails → mark as error, needs manual review

4. **Result** — legally distinct post that captures what worked, but is 100% yours

### Generating an Inspired Draft from the Dashboard

1. Go to **Inspiration** tab
2. Browse trending posts from your watchlist
3. Find one you like (hook style, engagement, structure)
4. Click **✨ Generate Inspired Draft**
5. System runs the inspector → new draft lands in **Queue** (status: `pending_review`)
6. Review the draft:
   - Check title/body for your voice
   - Verify images are your photos (not stolen)
   - Check `inspiration_note` field (explains the lineage)
7. Edit if needed, then approve to schedule

---

## Troubleshooting

### Issue: "Cookie Expired"

**Symptom:** Posts fail with error `cookie expired` or `account abnormal`.

**Fix:**
1. Re-run login script:
   ```powershell
   uv run python scripts/login.py --account banna
   ```
   OR
   ```powershell
   uv run python scripts/login.py --account stock
   ```
2. Scan QR code again
3. Cookies are refreshed; scheduler resumes normally

---

### Issue: Posts Not Publishing at Scheduled Time

**Symptom:** Draft is scheduled for 10:00 AM, but it's now 10:30 AM and still not posted.

**Causes & fixes:**

1. **Scheduler not running**
   - Check if the daemon is up:
     ```powershell
     # In a separate PowerShell window
     uv run python -m xhs_op.schedule.jobs
     ```
   - Should show `INFO: publish_due: found X due draft(s)` every 5 min in logs

2. **Cadence check failed** — last post was too recent
   - Go to **Schedule** tab
   - Check if cadence indicator shows red ✗ for your account
   - Wait 90 min from last published post, then the draft will post

3. **Jitter window hasn't arrived yet**
   - Jitter is ±15 min, so a 10:00 AM scheduled draft might post as late as 10:15 AM
   - Check logs for `_publish_one: draft=X too early`

4. **Image validation failed**
   - Draft requires ≥1 image for XHS
   - If image is missing, draft is skipped
   - Add an image in Queue, re-approve

---

### Issue: "x-s Signature Mismatch"

**Symptom:** Error message includes `x-s signature` or `300015`.

**Cause:** XHS updated their API signature scheme, and the cookie-based ReaJason/xhs library couldn't adapt in time.

**Fix (automatic):**
1. Scheduler detects the error
2. Falls back to **Playwright fallback** (browser-based posting)
3. You see a Chromium window open, fill the form, and click publish
4. Once browser publishes succeeds, draft is marked published

**If Playwright fallback also fails:**
1. Check your XHS account — you may have been temporarily locked (anti-bot)
2. Log in manually on XHS.com to verify account status
3. Wait 24 hours, re-run login script to refresh cookies
4. Try re-posting

---

### Issue: AI Draft Quality Is Low

**Symptom:** Generated title/body is generic, off-brand, or doesn't match the original tweet's meaning.

**Causes & fixes:**

1. **Wrong persona selected**
   - Check the persona in Queue (e.g., is it `stock_digest` when it should be `stock_hottake`?)
   - `stock_hottake` is for recent, high-engagement tweets (< 2 hours old, score > threshold)
   - Edit the draft and change persona, then regenerate

2. **Source content is low-quality**
   - If the source tweet is vague, the LLM can't do better
   - Edit the draft manually to improve clarity

3. **Model routing is wrong**
   - Check `src/xhs_op/config.py` for model IDs
   - Ensure your API keys are set and valid in `.env`
   - Test with:
     ```powershell
     uv run python -m xhs_op.generate.llm --persona stock_digest --prompt "test message"
     ```

4. **LM Studio is down (if using local model)**
   - If `model_routing` includes `lm_studio/...`, ensure LM Studio is running
   - Start it: open LM Studio app, load a model, start the server
   - Default URL: `http://localhost:1234/v1`

---

### Issue: Comments Not Appearing in Dashboard

**Symptom:** You have new comments on XHS, but they don't show up in the Comments tab.

**Causes & fixes:**

1. **Engagement poller not running**
   - The scheduler runs the comment poller every 15 min
   - Ensure the scheduler daemon is up (see scheduler not running fix above)

2. **Comment is on a post from before automation started**
   - The system only tracks posts created via this automation (in `posts` table)
   - Comments on old posts won't be captured

3. **Comment is spam, and you turned off drafting for spam**
   - Spam comments are inserted into DB with `drafted_reply=None`
   - Check the Comments tab for `intent='spam'` rows (they won't have draft suggestions)

---

### Issue: Can't Create Cookies (QR Code Won't Scan)

**Symptom:** Run `scripts/login.py`, see Chromium open, QR code appears, but scanning fails.

**Causes & fixes:**

1. **Wrong proxy configured**
   - For banna account, ensure you're using a **Yunnan-region residential proxy**
   - XHS will reject logins from non-China IPs
   - Test proxy: open `http://httpbin.org/ip` via proxy, confirm location is Yunnan

2. **Account is locked (anti-bot)**
   - If you've logged in manually too many times or from multiple locations, XHS may lock the account
   - Solution: wait 24 hours, verify on XHS.com, then try again

3. **Two-factor auth required**
   - Some XHS accounts require SMS 2FA
   - The headless script can't handle SMS
   - Solution: log in manually on XHS.com, complete 2FA, then run the script

---

### Issue: Residential Proxy Not Working

**Symptom:** Posts fail with error `proxy connection failed` or timeout.

**Causes & fixes:**

1. **Proxy URL format is wrong**
   - Should be: `socks5://username:password@host:port`
   - OR: `http://username:password@host:port`
   - Check your proxy provider's documentation

2. **Proxy credentials are expired**
   - Residential proxy services rotate IP pools
   - Log into your provider's dashboard, refresh credentials, update `.env`

3. **Proxy is overloaded**
   - Try a different proxy server from the same provider
   - Contact provider support

**To test proxy:**
```powershell
uv run python -c "
import httpx
proxy = 'socks5://user:pass@host:port'
with httpx.Client(proxies=proxy) as client:
    r = client.get('http://httpbin.org/ip')
    print(r.json())
"
```

---

### Issue: Image Generation Fails

**Symptom:** Error like `gemini API limit exceeded` or `OpenAI image endpoint timeout`.

**Causes & fixes:**

1. **API key is invalid or quota exceeded**
   - Gemini: check you have free trial credit or paid plan activated
   - OpenAI: ensure account has image generation enabled + credit
   - Verify keys in `.env`

2. **Rate limit**
   - Gemini allows ~5 images/min
   - OpenAI allows ~3 images/min
   - If you're generating many images quickly, wait a minute and retry

3. **Prompt is too complex**
   - Keep prompts short & specific:
     - ✅ "rainforest villa at sunset, cinematic"
     - ❌ "a villa in the rainforest at sunset with clouds and a girl sitting by the pool wearing a white dress and..."
   - Simplify, then retry

---

### Issue: Dashboard Won't Start

**Symptom:** Run `streamlit run ...`, get error or hangs.

**Causes & fixes:**

1. **Port 8501 is in use**
   - Close any other Streamlit apps
   - OR start on different port:
     ```powershell
     uv run streamlit run src/xhs_op/dashboard/app.py --server.port 8502
     ```

2. **Database is locked**
   - Another process (scheduler) has the DB open
   - Close the scheduler, then start dashboard
   - OR wait for scheduler's current cycle to complete (usually <30 sec)

3. **Missing API keys in `.env`**
   - Dashboard loads fine, but LLM calls fail
   - Check logs for which key is missing
   - Add it to `.env`, restart dashboard

---

## Tips & Best Practices

### Morning Workflow (5 minutes)

1. Open dashboard → **Queue** tab
2. Skim newly generated drafts (from overnight X tweets + XHS trends)
3. Approve 1–2 best drafts for morning/midday posting
4. Close dashboard

### Evening Workflow (5 minutes)

1. Open dashboard → **Queue** tab
2. Approve 1–2 drafts for evening/next-morning posting
3. Go to **Comments** tab, review & approve replies to today's posts
4. Close dashboard

### Weekly Tuning (30 minutes)

1. Open dashboard → **Analytics** tab
2. Check which post types perform best (villa / stock_digest / stock_hottake)
3. Edit personas if needed (`src/xhs_op/generate/personas/`)
4. Update watchlists (`data/x_watchlist.yaml`, `data/competitor_watchlist.yaml`) based on what's trending
5. Restart scheduler for changes to take effect

### Monthly Refresh

1. Refresh cookies (scripts/login.py) if older than 5 days
2. Update model routing if you switch providers
3. Review & update proxy provider (test via proxy test command)
4. Rotate persona personalities (keep them fresh)

---

## Reference: Files & Directories

```
xhs_op/
├── src/xhs_op/
│   ├── config.py               # Settings, API keys, model routing
│   ├── db.py                   # Database schema & helpers
│   ├── dashboard/app.py        # Streamlit web interface (this is your UI)
│   ├── sources/                # Content fetching
│   │   ├── x_scraper.py       # X (Twitter) account / trend monitoring
│   │   ├── xhs_competitor.py  # XHS trending post scraping
│   │   └── rss.py             # News feed aggregation
│   ├── generate/               # AI drafting
│   │   ├── llm.py             # LLM routing (Claude, GPT, Qwen, LM Studio)
│   │   ├── image.py           # Image generation (Gemini, OpenAI)
│   │   ├── translator.py      # X → XHS adaptation
│   │   ├── inspirer.py        # Competitor → Inspired drafts
│   │   └── personas/          # Brand voices (stock_digest.md, villa.md, etc.)
│   ├── publish/                # Publishing
│   │   ├── xhs_client.py      # XHS API wrapper (ReaJason/xhs)
│   │   └── playwright_fallback.py  # Browser-based fallback
│   ├── engage/                 # Comment handling
│   │   └── reply.py           # Notification polling & reply drafting
│   └── schedule/               # Automation
│       ├── jobs.py            # APScheduler job definitions
│       └── cadence.py         # Posting rules (90-min gap, daily quota)
├── data/
│   ├── xhs_op.db              # SQLite database (your content state)
│   ├── x_watchlist.yaml       # X accounts & search terms to monitor
│   ├── competitor_watchlist.yaml # XHS keywords to track
│   ├── feeds.yaml             # RSS feeds
│   ├── cookies/               # XHS account session cookies (git-ignored)
│   │   ├── banna.json
│   │   └── stock.json
│   └── assets/
│       ├── villa_photos/      # Your real property photos
│       └── generated/         # AI-generated images
├── scripts/
│   ├── login.py               # Interactive QR-code login for each account
│   ├── smoke_publish.py       # Test publishing (post then delete)
│   └── run_dashboard.py       # Helper script to start dashboard
├── .env                       # API keys & proxies (git-ignored)
├── .env.example               # Template (safe to commit)
├── pyproject.toml             # Dependencies (uv package list)
└── README.md                  # Tech overview
```

---

## Support & Getting Help

### Common Commands

```powershell
# Start the dashboard
uv run streamlit run src/xhs_op/dashboard/app.py

# Start the scheduler daemon (auto-posts scheduled drafts, polls comments every 15 min)
uv run python -m xhs_op.schedule.jobs

# Run scheduler once (for testing, then exit)
uv run python -m xhs_op.schedule.jobs --once

# Refresh cookies
uv run python scripts/login.py --account banna
uv run python scripts/login.py --account stock

# Test a model
uv run python -m xhs_op.generate.llm --persona villa --prompt "test"

# Test image generation
uv run python -m xhs_op.generate.image --prompt "villa at sunset"

# Initialize database
uv run python -c "from xhs_op.db import init_db; init_db()"
```

### Logs & Debugging

- **Dashboard logs** — printed to terminal when you run `streamlit run ...`
- **Scheduler logs** — printed to terminal when you run `python -m xhs_op.schedule.jobs`
- **Database queries** — check `data/xhs_op.db` with SQLite viewer or `sqlite3 data/xhs_op.db`

---

## Version History

**1.0 (May 2026)**
- Initial release
- Dual-account automation (banna-villa, stock-ai-digest)
- Auto-drafting from X, XHS, RSS
- Scheduler with cadence enforcement
- Comment engagement & auto-replies
- Dashboard for manual review & approval
- Image generation (Gemini + OpenAI fallback)
- LLM routing (Claude, GPT, Qwen, LM Studio)

---

**Happy posting! 🚀**

For updates & feature requests, visit the project repo or contact the team.
