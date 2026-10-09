<div align="center">

# unfold

### Your [Fold.money](https://fold.money) data — unfolded.

**Sync, analyze, export and *own* your Fold account data. Locally. Offline. Privately.**
And ask Claude about your money while you're at it.

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/Claude-MCP%20server-8A5CF6.svg)](#-ask-claude-about-your-money-mcp)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#-contributing)
[![Unofficial](https://img.shields.io/badge/unofficial-your%20own%20account%20only-lightgrey.svg)](#-disclaimer)

</div>

---

Fold is a genuinely great money app. But your transactions, balances and
investments live inside *their* servers and *their* UI. **unfold** is a tiny,
friendly CLI that logs into your own account (QR or OTP, once), pulls your data
into a local SQLite database, and turns it into spending insights, a local HTML
dashboard, Excel/CSV exports, a tax report — and a read-only MCP server so
**Claude can answer questions about your money**. Nothing leaves your machine.

```bash
./fold login      # scan a QR with the Fold app (or SMS OTP)
./fold            # incremental sync → local DB + JSON/CSV
./fold summary    # spending insights, offline, in your terminal
./fold report     # a local HTML dashboard you actually want to look at
```

---

## ✨ Why you'll want this

- 🔒 **Privacy-first by design.** Your financial data and credentials stay on
  *your* disk. No cloud, no telemetry, no third party. The DB and secrets are
  git-ignored out of the box.
- ⚡ **Fast, refresh-aware incremental sync.** Stops the moment it hits known
  transactions, caches category labels, and *knows* that Fold only re-pulls your
  bank data ~once a day — so routine reruns finish in ~1 second and never waste a
  fetch.
- 🧠 **UPI-aware enrichment.** Most Indian spend is opaque P2P UPI. unfold
  extracts the real **counterparty** from each narration, keyword-tags a
  category, and detects **recurring payments** — all locally.
- 🤖 **Ask Claude about your money.** A read-only MCP server exposes your local
  DB to Claude: *"summarize my August spending"*, *"any unusual charges?"*,
  *"compare June and August"*, *"FY 2025-26 tax report"*.
- 📊 **Outputs you'll use.** Terminal insights, a self-contained HTML dashboard,
  a 5-sheet Excel workbook, a Lunch Money import CSV, and ledger-cli/hledger
  double-entry output.
- 🇮🇳 **Built for Indian money.** UPI app detection (PhonePe/GPay/Paytm/…),
  ₹ formatting, and an **Apr–Mar financial-year** tax report.

---

## 📺 What it looks like

```text
$ ./fold summary

Fold · as of 08 Oct 14:39 · balance ₹1,24,380
──────────────────────────────────────────────
Lifetime   in ₹18,42,100   out ₹17,17,720   net +₹1,24,380   (savings rate 7%)

Last 6 months            in        out        net
  2026-05           ₹1,61,000   ₹1,48,200   +₹12,800
  2026-06           ₹1,58,400   ₹1,51,900    +₹6,500
  2026-07           ₹1,62,100   ₹1,39,400   +₹22,700
  …

Top categories (this month)      Top counterparties        Recurring
  Food & Drinks   ₹14,210          Landlord    ₹18,000       Netflix   (6mo) ₹649
  Shopping        ₹ 9,880          Swiggy      ₹ 4,120       Rent      (6mo) ₹18,000
  Transport       ₹ 6,540          Mom         ₹ 3,000       Gym       (5mo) ₹1,499
```

*(Numbers above are illustrative. `./fold report` renders the same data as an
offline HTML dashboard with charts.)*

---

## 🚀 Quick start

```bash
git clone https://github.com/<you>/unfold.git
cd unfold

# one-time: create the venv (requests + websocket-client + qrcode; mcp for the MCP server)
python3 -m venv .venv
.venv/bin/pip install requests websocket-client qrcode mcp openpyxl

./fold login          # guided: pick QR (scan) or OTP (SMS)
./fold                # sync (default): pull new txns → DB + JSON/CSV
./fold summary        # spending insights from the local DB (no network)
```

The `./fold` launcher auto-uses the project venv. That's it — you're running.

---

## 🧰 Commands

| Command | What it does |
|---|---|
| `./fold` / `./fold sync` | **Incremental sync.** Fetches only NEW transactions into `fold_export/fold.db`, writes JSON/CSV snapshots (skipped when nothing changed). |
| `./fold login` | Log in. Guided menu, or `--method qr\|otp`. |
| `./fold summary [--month]` | Rich terminal insights: balances, lifetime in/out/net + savings rate, 6-month trend, spend-by-category, top counterparties, recurring payments. Offline. |
| `./fold report [--open]` | **Local HTML dashboard** — stat tiles, monthly income/expense chart, category & counterparty bars, recurring + biggest-expense tables. Data never leaves your machine. |
| `./fold export [--lunchmoney]` | **Excel workbook** (.xlsx, 5 sheets); `--lunchmoney` also writes a Lunch Money import CSV. |
| `./fold tax [--fy 2025-26]` | Indian financial-year (Apr–Mar) income/expense report. |
| `./fold compare M1 M2` | Two months side by side with per-category deltas. |
| `./fold unusual` | Flags transactions ≥2.5× a payee's average (anomalies). |
| `./fold alias [suggest]` | Name your frequent UPI VPAs → category (`fold_aliases.json`). `suggest` seeds your top uncategorized payees. |
| `./fold networth` | Fetch investments (mutual funds w/ XIRR, stocks, FD, NPS) + net worth (online). |
| `./fold status` | Whether Fold has fresh data to sync: per-account last-pull vs your last local sync. |
| `./fold watch [15m]` | Sync repeatedly on a schedule. |

**Useful sync/watch flags:** `--full` (re-pull everything), `--export` (rewrite
snapshots anyway), `--ledger PATH` (append to a ledger-cli/hledger file,
deduped), `--balance-history`, `--no-db`, `--max-pages N`, `--limit N`, `-v`.

---

## 🔑 Auth

- **QR** — `./fold login --method qr` → scan the terminal QR with the Fold app
  (Profile → Web app). Logs in as its own web device.
- **OTP** — `./fold login --method otp [--phone +9198XXXXXXXX]` → SMS code.

Either way a ~90-day refresh token is saved to `fold_secrets.json` and unfold
self-refreshes headless afterwards. Re-auth only on expiry or `--login`. Revoke
any time from the app's **Web app** session.

---

## 🤖 Ask Claude about your money (MCP)

`fold_mcp.py` is a **read-only** MCP server over your local DB. Register it once:

```bash
claude mcp add fold -- "$PWD/.venv/bin/python" "$PWD/fold_mcp.py"
```

Then just ask:

> *"Summarize my August spending."*
> *"Compare June and August."*
> *"Any unusual charges this month?"*
> *"Search my Swiggy transactions."*
> *"Give me my FY 2025-26 tax report."*

Tools exposed: `summary`, `monthly_trend`, `tax`, `compare`, `unusual`,
`search`. Read-only — Claude can analyze, never modify or spend.

---

## 🏷️ Naming your UPI payees (aliases)

Most spend is P2P UPI to opaque VPAs. Fix the categories once:

```bash
./fold alias suggest        # seeds fold_aliases.json with your top uncategorized payees
# edit it: "ayush92u3axl": { "name": "Ayush", "category": "Rent" }
./fold summary              # now uses your names/categories everywhere
```

Aliases apply across `summary`, `report`, `export`, `tax`, `compare` **and** the
MCP server. See [`fold_aliases.example.json`](fold_aliases.example.json).

---

## 🔗 Integrations

- **Lunch Money** — `./fold export --lunchmoney` → a ready-to-import CSV.
- **Excel** — `./fold export` → a 5-sheet `.xlsx` workbook.
- **Actual Budget / hledger / ledger** — `./fold --ledger fold.ledger`
  (double-entry, deduped).

---

## 🧩 How it works

Fold pulls your bank data from the Account Aggregator (Finvu / CAMS) **once a
day** (plus a few ad-hoc refreshes a month). unfold records Fold's
`last_fetched_at` after each sync, so the next run can **skip instantly** if
nothing was re-pulled — no wasted transaction fetch. `--full` forces a re-pull
(e.g. to catch manually-added or credit-card transactions).

Because the raw data is mostly opaque UPI narrations, `summary` / `report` /
`export` run a local **enrichment** pass: extract the counterparty, keyword-tag a
coarse category, and derive analytics (monthly trend, savings rate,
recurring-payment detection, top payees, biggest expenses). All offline over
`fold.db` — re-analyze any time without re-syncing.

**Output lives in `fold_export/`:** `fold.db` (source of truth, incremental),
`transactions.(json|csv)` (24 rich columns), `accounts.(json|csv)`,
`profile.json`, optional `balance-history.json`, and your ledger file.

---

## 🔐 Security & privacy

`fold_secrets.json` (credentials) and `fold_export/fold.db` (your financial
history) hold live data. They are **git-ignored by default** and never leave
your machine. unfold makes no outbound calls except to Fold's own API, as *you*.
Revoke access any time via the app's **Web app** session or `./fold login`.

> If you fork or share this repo, double-check that `fold_secrets.json`,
> `fold_aliases.json`, `fold_login_qr.svg` and `fold_export/` stay out of your
> commits — the included `.gitignore` already handles all of them.

---

## 📦 Requirements

- Python 3.10+
- `requests`, `websocket-client`, `qrcode` (core), `openpyxl` (Excel export),
  `mcp` (Claude MCP server)
- A Fold account (it's your data).

---

## 🤝 Contributing

Issues and PRs welcome — new category rules, UPI app mappings, export formats,
and MCP tools are all easy wins. Please keep it **read-mostly and local-first**;
unfold should never be able to move money, only read and analyze it.

---

## ⚠️ Disclaimer

**unfold is an unofficial, independent project. It is not affiliated with,
endorsed by, or supported by Fold.** It talks to Fold's private API *as your own
authenticated device*, strictly for accessing **your own** account data for
personal use. You are responsible for complying with Fold's Terms of Service.
APIs can change without notice and may break this tool. Provided **as-is, with no
warranty**. Never share your `fold_secrets.json`.

---

## 📄 License

[MIT](LICENSE) — do what you like, no warranty.

<div align="center">
<sub>Built for people who believe you should be able to <b>unfold</b> your own data.</sub>
</div>
