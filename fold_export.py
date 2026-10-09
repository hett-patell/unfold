#!/usr/bin/env python3
"""
fold — export, sync & summarize YOUR OWN Fold (fold.money) data.

Commands (run via the ./fold launcher, which uses the venv):
    ./fold                 # = sync (default): incremental sync + snapshots
    ./fold login           # log in (guided: QR or OTP)
    ./fold summary         # spending insights from the local DB (no network)
    ./fold watch 15m       # keep syncing on a schedule

Auth is QR (scan once with the Fold app) or OTP (SMS). The script logs in as its
own device, saves a ~90-day refresh token, and self-refreshes headless after.
"""

import argparse
import csv
import datetime as dt
import json
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import fold_analytics as fa

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Use ./fold (or .venv/bin/python fold_export.py)")

API_BASE = "https://api.fold.money"
WS_QR = "wss://api.fold.money/api/v1/auth/web/generate_qr_code"
DEFAULT_UA = ("Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Mobile Safari/537.36")

EP_QR_LOGIN = "/api/v1/auth/web/qr_code_login"
EP_REFRESH = "/api/v1/auth/tokens/refresh"
EP_OTP = "/api/v1/auth/otp"
EP_OTP_VERIFY = "/api/v1/auth/otp/verify"
EP_ME = "/api/v2/users/me"
EP_ACCOUNTS = "/api/v1/aa/data"
EP_TRANSACTIONS = "/api/v3/users/{uid}/transactions"
EP_AVAILABLE_SINCE = "/api/v1/users/{uid}/transactions/available_since"
EP_CATEGORIES = "/api/v2/categories/search"
EP_BALANCE_GRAPH = "/api/v1/users/{uid}/graphs/total_balance"
EP_INVEST_AGG = "/api/v2/users/{uid}/investments/aggregation"
EP_MF = "/api/v2/users/{uid}/investments/mutual-funds/schemes"
EP_DEMAT = "/api/v2/users/{uid}/investments/demat/holdings/aggregated"
EP_EPF = "/api/v2/users/{uid}/epf/account"

# categories excluded from spend/income insights (transfers, ATM, etc.)
NON_SPEND = ("Self Transfer", "Cash Withdrawal")

TXN_COLUMNS = [
    "uuid", "txn_timestamp", "txn_date", "type", "mode", "amount", "currency",
    "current_balance", "category_id", "subcategory_id", "category_name",
    "subcategory_name", "merchant_name", "merchant_type", "narration",
    "account_id", "transaction_id", "kind", "refund_status", "is_bookmarked",
    "excluded_from_cash_flow", "split_type", "parent_transaction_id", "summary",
]


class FoldError(Exception):
    pass


# ---- helpers ---------------------------------------------------------------
def parse_ts(s: str) -> float:
    if not s:
        return 0.0
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s, fmt = f"{head}.{frac[:6]}", "%Y-%m-%dT%H:%M:%S.%f"
    else:
        fmt = "%Y-%m-%dT%H:%M:%S"
    return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc).timestamp()


def parse_duration(s: str) -> int:
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", s or "")
    if not m:
        raise FoldError(f"Bad interval '{s}'. Use e.g. 30s, 5m, 1h, 1d.")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def inr(n) -> str:
    return f"₹{(n or 0):,.0f}"


def humts(iso) -> str:
    """Fold timestamps are UTC; show them in local (machine) time."""
    if not iso:
        return "never"
    try:
        return dt.datetime.fromtimestamp(parse_ts(iso)).strftime("%d %b %H:%M")
    except Exception:
        return str(iso)[:16]


def load_secrets(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_secrets(path: Path, secrets: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(secrets, indent=2))
    tmp.replace(path)


def labelled_row(t: dict, cat_map: dict, sub_map: dict) -> dict:
    cat, merch, refund = (t.get("category") or {}), (t.get("merchant") or {}), (t.get("refund") or {})
    return {
        "uuid": t.get("uuid"), "txn_timestamp": t.get("txn_timestamp"),
        "txn_date": (t.get("txn_timestamp") or "")[:10], "type": t.get("type"),
        "mode": t.get("mode"), "amount": t.get("amount"), "currency": t.get("currency"),
        "current_balance": t.get("current_balance"), "category_id": cat.get("id"),
        "subcategory_id": cat.get("subcategory_id"),
        "category_name": cat_map.get(cat.get("id")),
        "subcategory_name": sub_map.get(cat.get("subcategory_id")),
        "merchant_name": merch.get("name"), "merchant_type": merch.get("type"),
        "narration": t.get("narration"), "account_id": t.get("account_id"),
        "transaction_id": t.get("transaction_id"), "kind": t.get("kind"),
        "refund_status": refund.get("status"),
        "is_bookmarked": 1 if t.get("is_bookmarked") else 0,
        "excluded_from_cash_flow": 1 if t.get("excluded_from_cash_flow") else 0,
        "split_type": t.get("split_type"),
        "parent_transaction_id": t.get("parent_transaction_id"),
        "summary": (t.get("summary") or "").strip(),
    }


# ---- sqlite store ----------------------------------------------------------
class FoldStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        types = {"amount": "REAL", "current_balance": "REAL",
                 "is_bookmarked": "INTEGER", "excluded_from_cash_flow": "INTEGER"}
        cols = ", ".join(f"{c} {types.get(c, 'TEXT')}" for c in TXN_COLUMNS)
        self.db.execute(f"CREATE TABLE IF NOT EXISTS transactions ({cols}, raw TEXT, PRIMARY KEY(uuid))")
        self.db.execute("CREATE TABLE IF NOT EXISTS accounts (uuid TEXT PRIMARY KEY, raw TEXT, updated_at TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        self.db.commit()

    def existing_uuids(self) -> set:
        return {r[0] for r in self.db.execute("SELECT uuid FROM transactions")}

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    def insert_txns(self, txns, cat_map, sub_map) -> None:
        cols = TXN_COLUMNS + ["raw"]
        ph = ",".join("?" * len(cols))
        rows = []
        for t in txns:
            row = labelled_row(t, cat_map, sub_map)
            rows.append([row[c] for c in TXN_COLUMNS] + [json.dumps(t, ensure_ascii=False)])
        self.db.executemany(f"INSERT OR REPLACE INTO transactions ({','.join(cols)}) VALUES ({ph})", rows)
        self.db.commit()

    def upsert_accounts(self, accounts) -> None:
        now = dt.datetime.now().isoformat()
        for a in accounts:
            self.db.execute("INSERT INTO accounts(uuid,raw,updated_at) VALUES(?,?,?) "
                            "ON CONFLICT(uuid) DO UPDATE SET raw=excluded.raw, updated_at=excluded.updated_at",
                            (a.get("uuid"), json.dumps(a, ensure_ascii=False), now))
        self.db.commit()

    def all_txns(self) -> list:
        return [json.loads(r[0]) for r in
                self.db.execute("SELECT raw FROM transactions ORDER BY txn_timestamp DESC")]

    def get_meta(self, k):
        r = self.db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return json.loads(r[0]) if r else None

    def set_meta(self, k, v):
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (k, json.dumps(v, ensure_ascii=False)))
        self.db.commit()

    def close(self):
        self.db.close()


# ---- ledger export ---------------------------------------------------------
def ledger_account_for(account_id, accounts_by_id) -> str:
    a = accounts_by_id.get(account_id) or {}
    fip = ((a.get("financial_information_provider") or {}).get("name") or "Account")
    name = f"Assets:Fold:{fip}"
    if a.get("masked_account_number"):
        name += f":{a['masked_account_number']}"
    return name


def ledger_entry(t, accounts_by_id, cat_map) -> str:
    date = (t.get("txn_timestamp") or "")[:10].replace("-", "/") or "1970/01/01"
    amt, cur = t.get("amount") or 0, t.get("currency") or "INR"
    catname = cat_map.get((t.get("category") or {}).get("id")) or "Uncategorized"
    merch = (t.get("merchant") or {}).get("name")
    desc = (merch or (t.get("summary") or "").strip()
            or (t.get("narration") or "")).replace("\n", " ").strip() or "Fold transaction"
    asset = ledger_account_for(t.get("account_id"), accounts_by_id)
    postings = ([(asset, amt), (f"Income:{catname}", -amt)] if t.get("type") == "INCOMING"
                else [(f"Expenses:{catname}", amt), (asset, -amt)])
    lines = [f"{date} {desc}  ; uuid: {t.get('uuid')}"]
    lines += [f"    {acct}  {val:.2f} {cur}" for acct, val in postings]
    return "\n".join(lines)


def ledger_sync(txns, path: Path, accounts_by_id, cat_map) -> int:
    have = set(re.findall(r"uuid:\s*([0-9a-fA-F-]{36})", path.read_text())) if path.exists() else set()
    new = sorted((t for t in txns if t.get("uuid") not in have),
                 key=lambda t: t.get("txn_timestamp") or "")
    if new:
        with path.open("a") as f:
            for t in new:
                f.write(ledger_entry(t, accounts_by_id, cat_map) + "\n\n")
    return len(new)


# ---- QR render -------------------------------------------------------------
def render_login_qr(uri: str, svg_path: Path) -> None:
    import qrcode
    sys.stdout.write("\033[2J\033[H")
    print("  Scan with the Fold app:  Profile -> Web app -> scan\n")
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    try:
        from qrcode.image.svg import SvgImage
        qrcode.make(uri, image_factory=SvgImage).save(str(svg_path))
        print(f"\n  (can't scan the terminal? open {svg_path} — refreshes ~30s)")
    except Exception:
        pass
    print("  Waiting for you to scan... (Ctrl+C to cancel)")


# ---- client ----------------------------------------------------------------
class FoldClient:
    def __init__(self, secrets, secrets_path, use_bearer=False, verbose=False,
                 force_login=False, login_method=None):
        self.secrets = secrets
        self.secrets_path = secrets_path
        self.use_bearer = use_bearer
        self.verbose = verbose
        self.force_login = force_login
        self.login_method = login_method  # None => ask
        self.cli_phone = None
        self.cli_otp = None
        self.user_id = secrets.get("user_id")
        self.access_token = None
        self.expires_at = 0.0
        self.cat_map = None
        self.sub_map = None
        self.session = requests.Session()
        if secrets.get("marble_cookie"):
            self.session.cookies.set("marble-cookie", secrets["marble_cookie"], domain=".fold.money")

    def _log(self, *a):
        if self.verbose:
            print("[fold]", *a, file=sys.stderr)

    def _device_hash(self) -> str:
        dh = self.secrets.get("device_hash")
        if not dh:
            dh = str(uuid.uuid4())
            self.secrets["device_hash"] = dh
            save_secrets(self.secrets_path, self.secrets)
        return dh

    def _headers(self, json_body=False) -> dict:
        h = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en-GB,en;q=0.9",
            "origin": "https://fold.money", "referer": "https://fold.money/",
            "user-agent": self.secrets.get("user_agent", DEFAULT_UA),
            "x-device-hash": self._device_hash(),
            "x-device-type": self.secrets.get("device_type", "Web"),
            "x-device-location": self.secrets.get("device_location", "India"),
            "x-request-id": str(uuid.uuid4()),
        }
        if json_body:
            h["content-type"] = "application/json"
        return h

    def _store_tokens(self, data: dict) -> None:
        self.access_token = data.get("access_token")
        if data.get("refresh_token"):
            self.secrets["refresh_token"] = data["refresh_token"]
        um = data.get("user_meta") or {}
        if um.get("uuid"):
            self.secrets["user_id"] = self.user_id = um["uuid"]
        self.expires_at = parse_ts(data.get("expires_at", "")) or (time.time() + 600)
        save_secrets(self.secrets_path, self.secrets)

    # -- login methods --
    def _choose_method(self) -> str:
        if not sys.stdin.isatty():
            return "qr"
        print("\nLog in to Fold:")
        print("  1) QR code — scan with the Fold app (recommended)")
        print("  2) OTP     — one-time code via SMS")
        return "otp" if input("Choose [1/2] (default 1): ").strip() == "2" else "qr"

    def _do_login(self) -> None:
        method = self.login_method or self._choose_method()
        if method == "otp":
            self.otp_login(self.cli_phone, self.cli_otp)
        else:
            self.qr_login()

    def qr_login(self) -> None:
        try:
            import websocket
            import qrcode  # noqa
        except ImportError:
            raise FoldError("QR login needs websocket-client + qrcode (use ./fold / the venv).")
        dh = self._device_hash()
        self._log("registered script device:", dh)
        svg_path = self.secrets_path.parent / "fold_login_qr.svg"
        hdr = [f"User-Agent: {self.secrets.get('user_agent', DEFAULT_UA)}",
               f"x-device-hash: {dh}", "x-device-type: Web", "x-device-location: India"]
        ws = websocket.create_connection(WS_QR, header=hdr, origin="https://fold.money", timeout=10)
        login_code, deadline = None, time.time() + 180
        try:
            ws.settimeout(5)
            while time.time() < deadline:
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    continue
                d = (json.loads(raw).get("data") or {})
                typ, payload = d.get("type"), (d.get("payload") or {})
                if typ == "qr":
                    render_login_qr(payload.get("qr_token", ""), svg_path)
                elif typ == "login_code":
                    login_code = payload.get("login_code")
                    break
        finally:
            ws.close()
        if not login_code:
            raise FoldError("QR login timed out / not scanned. Try again.")
        sys.stdout.write("\033[2J\033[H")
        print("Scanned. Finishing login...")
        r = self.session.post(f"{API_BASE}{EP_QR_LOGIN}", headers=self._headers(True),
                              json={"login_code": login_code}, timeout=30)
        if r.status_code != 200:
            raise FoldError(f"qr_code_login failed (HTTP {r.status_code}): {r.text[:300]}")
        self._store_tokens((r.json() or {}).get("data") or {})
        svg_path.unlink(missing_ok=True)
        self._log("logged in via QR; refresh token saved.")

    def otp_login(self, phone=None, otp=None) -> None:
        phone = (phone or self.secrets.get("phone")
                 or input("Phone number (e.g. +9198XXXXXXXX): ")).strip()
        if not phone.startswith("+"):
            phone = "+91" + phone.lstrip("0")
        self._log("OTP login as device", self._device_hash(), "phone", phone)
        r = self.session.post(f"{API_BASE}{EP_OTP}", headers=self._headers(True),
                              json={"phone": phone, "channel": "sms"}, timeout=30)
        if r.status_code != 200:
            raise FoldError(f"OTP send failed (HTTP {r.status_code}): {r.text[:200]}")
        print(f"OTP sent to {phone} via SMS.")
        code = (otp or input("Enter the OTP you received: ")).strip()
        r = self.session.post(f"{API_BASE}{EP_OTP_VERIFY}", headers=self._headers(True),
                              json={"phone": phone, "otp": code}, timeout=30)
        if r.status_code != 200:
            raise FoldError(f"OTP verify failed (HTTP {r.status_code}): {r.text[:200]}")
        self._store_tokens((r.json() or {}).get("data") or {})
        self._log("logged in via OTP; refresh token saved.")

    def refresh(self) -> None:
        if not self.secrets.get("refresh_token"):
            raise FoldError("No refresh_token to refresh with.")
        self._log("refreshing session...")
        r = self.session.post(f"{API_BASE}{EP_REFRESH}", headers=self._headers(True),
                              json={"refresh_token": self.secrets["refresh_token"]}, timeout=30)
        if r.status_code != 200:
            raise FoldError(f"Refresh failed (HTTP {r.status_code}): {r.text[:200]}")
        self._store_tokens((r.json() or {}).get("data") or {})

    def bootstrap(self) -> None:
        if self.secrets.get("refresh_token") and not self.force_login:
            try:
                self.refresh()
                return
            except FoldError as e:
                print(f"[fold] saved session didn't work ({e}); logging in again.", file=sys.stderr)
        if not sys.stdin.isatty() and not (self.cli_phone and self.cli_otp):
            raise FoldError("No valid saved session. Run interactively to log in, or "
                            "pass --method otp --phone <p> --otp <code>.")
        self._do_login()

    def get(self, path: str, params=None) -> dict:
        for attempt in (1, 2):
            headers = self._headers()
            if self.use_bearer and self.access_token:
                headers["authorization"] = f"Bearer {self.access_token}"
            r = self.session.get(f"{API_BASE}{path}", headers=headers, params=params, timeout=30)
            if r.status_code == 401 and attempt == 1 and self.secrets.get("refresh_token"):
                self._log(f"401 on {path} -> refresh + retry")
                self.refresh()
                continue
            if r.status_code != 200:
                raise FoldError(f"GET {path} failed (HTTP {r.status_code}): {r.text[:200]}")
            return r.json()
        raise FoldError(f"GET {path} failed after retry.")

    # -- data --
    def ensure_categories(self, store=None):
        if self.cat_map is not None:
            return self.cat_map, self.sub_map
        if store and (cached := store.get_meta("categories")):
            self.cat_map, self.sub_map = cached.get("cat", {}), cached.get("sub", {})
            return self.cat_map, self.sub_map
        cats, subs = {}, {}
        for kind in ("INCOMING", "OUTGOING"):
            data = self.get(EP_CATEGORIES, params={"type": kind}).get("data", {})
            for c in data.get("categories", []) or []:
                cats[c["uuid"]] = c["name"]
                for ic in c.get("icons", []) or []:
                    subs[ic["uuid"]] = ic.get("display_name")
        self.cat_map, self.sub_map = cats, subs
        if store:
            store.set_meta("categories", {"cat": cats, "sub": subs})
        return cats, subs

    def fetch_profile(self):
        return self.get(EP_ME).get("data", {})

    def fetch_accounts(self):
        return self.get(EP_ACCOUNTS).get("data", {})

    def fetch_available_since(self):
        try:
            return self.get(EP_AVAILABLE_SINCE.format(uid=self.user_id)).get("data", {})
        except FoldError as e:
            self._log("available_since unavailable:", e)
            return {}

    def fetch_balance_history(self, ids, start, end):
        params = [("account_id[]", a) for a in ids] + [("start", start), ("end", end)]
        return self.get(EP_BALANCE_GRAPH.format(uid=self.user_id), params=params).get("data", {})

    def fetch_investments(self):
        out = {}
        for key, ep in (("aggregation", EP_INVEST_AGG), ("mutual_funds", EP_MF),
                        ("demat", EP_DEMAT), ("epf", EP_EPF)):
            try:
                out[key] = self.get(ep.format(uid=self.user_id)).get("data")
            except FoldError as e:
                out[key] = {"_error": str(e)[:140]}
        return out

    def _page(self, after, limit):
        params = {"limit": limit}
        if after:
            params["after"] = after
        d = self.get(EP_TRANSACTIONS.format(uid=self.user_id), params=params).get("data", {})
        return (d.get("transactions") or []), d.get("after")

    def sync_transactions(self, store, limit=100, full=False, max_pages=None, pause=0.0):
        """Incremental: stop once a page adds nothing new (in-memory dedup)."""
        self.ensure_categories(store)
        known = store.existing_uuids()
        after, page, new_total, fetched = None, 0, 0, 0
        while True:
            batch, after = self._page(after, limit)
            fetched += len(batch)
            fresh = [t for t in batch if t.get("uuid") not in known]
            if fresh:
                store.insert_txns(fresh, self.cat_map, self.sub_map)
                known.update(t.get("uuid") for t in fresh)
            new_total += len(fresh)
            page += 1
            self._log(f"page {page}: {len(batch)} fetched, {len(fresh)} new (db {store.count()})")
            if not batch or not after:
                break
            if not full and not fresh:
                self._log("caught up with existing data; stopping (--full to re-pull)")
                break
            if max_pages and page >= max_pages:
                break
            if pause:
                time.sleep(pause)
        return new_total, fetched

    def fetch_all_transactions(self, limit=100, max_pages=None, pause=0.0):
        self.ensure_categories()
        out, after, page = [], None, 0
        while True:
            batch, after = self._page(after, limit)
            out.extend(batch)
            page += 1
            self._log(f"page {page}: {len(batch)} fetched (total {len(out)})")
            if not batch or not after or (max_pages and page >= max_pages):
                break
            if pause:
                time.sleep(pause)
        return out


# ---- writers ---------------------------------------------------------------
def write_json(obj, path: Path):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def write_transactions_csv(txns, path, cat_map, sub_map):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=TXN_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for t in txns:
            w.writerow(labelled_row(t, cat_map, sub_map))


def write_accounts_csv(accounts, path):
    cols = ["bank", "holder_name", "masked_account_number", "type", "current_balance",
            "currency", "ifsc_code", "last_fetched_at", "uuid"]
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for a in accounts:
            fip = a.get("financial_information_provider") or {}
            w.writerow([fip.get("name"), a.get("holder_name"), a.get("masked_account_number"),
                        a.get("type"), a.get("current_balance"), a.get("currency"),
                        a.get("ifsc_code"), a.get("last_fetched_at"), a.get("uuid")])


# ---- sync command ----------------------------------------------------------
def do_sync(client, args, db_path, fixed_names):
    args.out.mkdir(parents=True, exist_ok=True)
    store = FoldStore(db_path) if db_path else None
    cat_map, sub_map = client.ensure_categories(store)

    profile = client.fetch_profile()
    acc_data = client.fetch_accounts()
    accounts = acc_data.get("accounts", []) or []
    accounts_by_id = {a["uuid"]: a for a in accounts}
    if store:
        store.upsert_accounts(accounts)
    client.fetch_available_since()

    cur_fetched = max((a.get("last_fetched_at") or "") for a in accounts) if accounts else ""
    prev_fetched = store.get_meta("last_fetched_max") if store else None
    if (store and prev_fetched and cur_fetched and cur_fetched <= prev_fetched
            and not args.full and store.count() > 0):
        total = sum((a.get("current_balance") or 0) for a in accounts)
        print(f"\n== Fold sync {dt.datetime.now():%H:%M:%S} ==")
        print(f"Fold last pulled bank data at {humts(cur_fetched)} — no new refresh since "
              f"your last sync, so there's nothing new.")
        print(f"{inr(total)} across {len(accounts)} accounts · {store.count()} txns in DB")
        print("(Fold refreshes ~once/day; use --full to re-pull anyway, or `./fold status`.)")
        store.close()
        return

    if store:
        new, _ = client.sync_transactions(store, limit=args.limit, full=args.full,
                                          max_pages=args.max_pages)
        txns = store.all_txns()
    else:
        txns = client.fetch_all_transactions(limit=args.limit, max_pages=args.max_pages)
        new = len(txns)

    do_write = (not store) or new > 0 or args.export
    if do_write:
        sfx = "" if fixed_names else "-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        write_json(profile, args.out / f"profile{sfx}.json")
        write_json(acc_data, args.out / f"accounts{sfx}.json")
        write_accounts_csv(accounts, args.out / f"accounts{sfx}.csv")
        write_json(txns, args.out / f"transactions{sfx}.json")
        write_transactions_csv(txns, args.out / f"transactions{sfx}.csv", cat_map, sub_map)
        if args.balance_history and accounts:
            today = dt.date.today()
            hist = client.fetch_balance_history([a["uuid"] for a in accounts],
                                                today.replace(day=1).isoformat(), today.isoformat())
            write_json(hist, args.out / f"balance-history{sfx}.json")

    ledger_added = ledger_sync(txns, args.ledger, accounts_by_id, cat_map) if args.ledger else None
    if store:
        store.set_meta("last_fetched_max", cur_fetched)
        store.set_meta("last_sync", dt.datetime.now().isoformat())
        store.close()

    user = profile.get("user") or {}
    total = sum((a.get("current_balance") or 0) for a in accounts)
    cur = accounts[0].get("currency", "") if accounts else ""
    print(f"\n== Fold sync {dt.datetime.now():%H:%M:%S} ==")
    print(f"{user.get('first_name','')} {user.get('last_name','')} · {inr(total)} across {len(accounts)} accounts")
    print(f"Fold last pulled bank data: {humts(cur_fetched)}")
    for a in accounts:
        fip = a.get("financial_information_provider") or {}
        print(f"  {fip.get('name'):<16} {a.get('masked_account_number'):<12} {a.get('current_balance'):>14,.2f}")
    tail = f"{new} new" if store else f"{new} fetched"
    print(f"Transactions: {len(txns)} total ({tail})"
          + ("" if do_write else " · snapshots unchanged, skipped write (use --export to force)"))
    if ledger_added is not None:
        print(f"Ledger: +{ledger_added} -> {args.ledger}")
    if store:
        print("Tip: `./fold summary` for spending insights.")


# ---- status command --------------------------------------------------------
def cmd_status(client, args):
    acc = client.fetch_accounts().get("accounts", []) or []
    cur = max((a.get("last_fetched_at") or "") for a in acc) if acc else ""
    total = sum((a.get("current_balance") or 0) for a in acc)
    print()
    print("  Fold \u00b7 status")
    print("  " + fa.hr())
    print(f"  Balance   {fa.inr(total)}   \u00b7   {len(acc)} accounts")
    for ln in fa.table(["Bank", "Account", "Balance", "Fold pulled"],
                       [[(a.get("financial_information_provider") or {}).get("name", ""),
                         a.get("masked_account_number", ""), fa.inr(a.get("current_balance")),
                         humts(a.get("last_fetched_at"))] for a in acc],
                       aligns=["l", "l", "r", "l"]):
        print(ln)
    db_path = args.db or args.out / "fold.db"
    print()
    if Path(db_path).exists():
        db = sqlite3.connect(str(db_path))
        n = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        dmin, dmax = db.execute("SELECT MIN(txn_date), MAX(txn_date) FROM transactions").fetchone()

        def meta(k):
            r = db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
            return json.loads(r[0]) if r else None
        last_sync, prev = meta("last_sync"), meta("last_fetched_max")
        db.close()
        ls = last_sync[:16].replace("T", " ") if last_sync else "never"
        print(f"  Local DB  {n:,} txns   \u00b7   {dmin} \u2192 {dmax}   \u00b7   synced {ls}")
        if prev and cur and cur <= prev:
            print("  \u2192 up to date (Fold hasn't re-pulled since your last sync)")
        else:
            print("  \u2192 new data available \u2014 run ./fold")
    else:
        print("  Local DB  none yet \u2014 run ./fold to create it.")


def _find_number(obj, keys):
    if isinstance(obj, dict):
        for k in keys:
            if isinstance(obj.get(k), (int, float)):
                return obj[k]
        for v in obj.values():
            r = _find_number(v, keys)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_number(v, keys)
            if r is not None:
                return r
    return None


def cmd_networth(client, args):
    acc = client.fetch_accounts().get("accounts", []) or []
    bank = sum((a.get("current_balance") or 0) for a in acc)
    inv = client.fetch_investments()
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    args.out.mkdir(parents=True, exist_ok=True)
    raw = args.out / f"investments-{ts}.json"
    raw.write_text(json.dumps(inv, indent=2, ensure_ascii=False))
    nw = fa.parse_investments(inv, bank)
    (args.out / "networth.json").write_text(json.dumps(nw, indent=2, ensure_ascii=False))
    mf = nw["mutual_funds"]
    print()
    print("  Fold \u00b7 net worth")
    print("  " + fa.hr())
    print(f"  Net worth   {fa.inr(nw['net_worth'])}")
    rows = [["Bank", fa.inr(nw["bank"]), f"{len(acc)} accounts"],
            ["Stocks", fa.inr(nw["stocks"]["value"]), f"{nw['stocks'].get('holdings') or 0} holdings"],
            ["Mutual funds", fa.inr(mf["value"]),
             f"XIRR {mf.get('xirr')}%  gain {fa.inr_c(mf.get('gain') or 0)}"]]
    if nw["fd"]:
        rows.append(["Fixed deposits", fa.inr(nw["fd"]), ""])
    if nw["nps"]:
        rows.append(["NPS", fa.inr(nw["nps"]), ""])
    for ln in fa.table(["", "Value", ""], rows, aligns=["l", "r", "l"], header=False):
        print(ln)
    if mf["schemes"]:
        print()
        print("  Mutual funds")
        for ln in fa.table(["Scheme", "Value", "Gain", "XIRR%"],
                           [[str(x.get("name") or "")[:30], fa.inr(x.get("value") or 0),
                             ("+" if (x.get("gain_loss") or 0) >= 0 else "") + fa.inr_c(x.get("gain_loss") or 0),
                             x.get("xirr") if x.get("xirr") is not None else "\u2014"] for x in mf["schemes"]],
                           aligns=["l", "r", "r", "r"]):
            print(ln)
    print()
    print(f"  raw \u2192 {raw}   \u00b7   ./fold report shows this in the dashboard")


# ---- offline commands (read the local DB, no network) ---------------------
def _db(args):
    db_path = args.db or args.out / "fold.db"
    if not Path(db_path).exists():
        raise FoldError(f"No database at {db_path}. Run `./fold sync` first.")
    return db_path


def cmd_summary(args):
    d = fa.compute(_db(args), month=args.month, aliases_path=args.aliases)
    lt = d["lifetime"]; net = lt["net"]
    sav = (net / lt["income"] * 100) if lt["income"] else 0
    rng = d["date_range"]
    print()
    print(f"  Fold \u00b7 summary \u00b7 {d['month']}")
    print("  " + fa.hr())
    print(f"  Balance     {fa.inr(d['total_balance'])}   \u00b7   {len(d['balances'])} accounts")
    print(f"  Lifetime    in {fa.inr_c(lt['income'])}    out {fa.inr_c(lt['expense'])}    "
          f"net {fa.inr_c(net)}    ({sav:.0f}% saved)")
    print(f"  Since       {rng[0]} \u2192 {rng[1]}   \u00b7   {d['n']:,} txns")
    print()
    print("  Last 6 months")
    for ln in fa.table(["Month", "In", "Out", "Net"],
                       [[m["month"], fa.inr_c(m["income"]), fa.inr_c(m["expense"]), fa.inr_c(m["net"])]
                        for m in d["monthly"][-6:]], aligns=["l", "r", "r", "r"]):
        print(ln)
    print()
    print(f"  Spending by category \u00b7 {d['month']}")
    fa.print_bars(d["cat_month"][:8])
    if d["merch_month"]:
        print()
        print(f"  Top counterparties \u00b7 {d['month']}")
        fa.print_bars(d["merch_month"][:8])
    if d["recurring"]:
        print()
        print("  Recurring (\u22653 months)")
        for ln in fa.table(["Payee", "Months", "Txns", "Total"],
                           [[str(cp)[:24], m, c, fa.inr_c(a)] for cp, m, a, c in d["recurring"][:8]],
                           aligns=["l", "r", "r", "r"]):
            print(ln)
    if d["by_mode"]:
        print()
        print("  By payment mode")
        for ln in fa.table(["Mode", "Spent", "Txns"],
                           [[k, fa.inr_c(a), c] for k, a, c in d["by_mode"][:5]],
                           aligns=["l", "r", "r"]):
            print(ln)
    fc = d["forecast"]
    if fc["spent"]:
        print()
        print(f"  This month  {fa.inr_c(fc['spent'])} in {fc['elapsed']}/{fc['days']}d "
              f"\u2192 proj {fa.inr_c(fc['projected'])} (3-mo avg {fa.inr_c(fc['avg3'])})")
    print()
    print("  ./fold report  \u00b7  ./fold export  \u00b7  ./fold networth")


def cmd_report(args):
    d = fa.compute(_db(args), month=args.month, aliases_path=args.aliases)
    nwf = args.out / "networth.json"
    nw = json.loads(nwf.read_text()) if nwf.exists() else None
    html = fa.render_report_html(d, dt.datetime.now().strftime("%d %b %Y %H:%M"), nw=nw)
    out = args.output or (args.out / "report.html")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(html)
    print(f"Dashboard written: {out}")
    if args.open:
        import webbrowser
        webbrowser.open(f"file://{Path(out).resolve()}")


def cmd_export(args):
    out = args.output or (args.out / "fold.xlsx")
    r = fa.export_xlsx(_db(args), out, aliases_path=args.aliases)
    print(f"Spreadsheet written: {out}  ({r['transactions']} txns \u00b7 sheets: {', '.join(r['sheets'])})")
    if getattr(args, "lunchmoney", False):
        lm = args.out / "fold-lunchmoney.csv"
        n = fa.export_lunchmoney_csv(_db(args), lm, aliases_path=args.aliases)
        print(f"Lunch Money CSV: {lm}  ({n} rows)")


def cmd_tax(args):
    t = fa.tax_year(_db(args), fy=args.fy, aliases_path=args.aliases)
    print()
    print(f"  Fold \u00b7 tax year {t['fy']}   ({t['start']} \u2192 {t['end']})")
    print("  " + fa.hr())
    print(f"  Income {fa.inr(t['income'])}    Expense {fa.inr(t['expense'])}    "
          f"Net {fa.inr(t['net'])}    ({t['n']:,} txns)")
    print()
    print("  By category")
    for ln in fa.table(["Category", "Amount"], [[k, fa.inr(v)] for k, v in t["by_category"][:15]],
                       aligns=["l", "r"]):
        print(ln)
    print()
    print("  By month")
    for ln in fa.table(["Month", "In", "Out"],
                       [[m, fa.inr_c(i), fa.inr_c(e)] for m, i, e in t["monthly"]],
                       aligns=["l", "r", "r"]):
        print(ln)


def cmd_compare(args):
    c = fa.compare_months(_db(args), args.month1, args.month2, aliases_path=args.aliases)
    di, de = c["income"][1] - c["income"][0], c["expense"][1] - c["expense"][0]
    print()
    print(f"  Fold \u00b7 {c['m1']} vs {c['m2']}")
    print("  " + fa.hr())
    for ln in fa.table(["", c["m1"], c["m2"], "\u0394"],
                       [["Income", fa.inr_c(c["income"][0]), fa.inr_c(c["income"][1]),
                         ("+" if di >= 0 else "") + fa.inr_c(di)],
                        ["Expense", fa.inr_c(c["expense"][0]), fa.inr_c(c["expense"][1]),
                         ("+" if de >= 0 else "") + fa.inr_c(de)]],
                       aligns=["l", "r", "r", "r"]):
        print(ln)
    print()
    print("  By category")
    for ln in fa.table(["Category", c["m1"], c["m2"], "\u0394"],
                       [[k, fa.inr_c(a), fa.inr_c(b), ("+" if dl >= 0 else "") + fa.inr_c(dl)]
                        for k, a, b, dl in c["by_category"][:15]], aligns=["l", "r", "r", "r"]):
        print(ln)


def cmd_unusual(args):
    u = fa.unusual(_db(args), aliases_path=args.aliases)
    print()
    print("  Fold \u00b7 unusual transactions  (\u2265 2.5\u00d7 payee average)")
    print("  " + fa.hr())
    if not u:
        print("  none flagged.")
        return
    for ln in fa.table(["Date", "Payee", "Amount", "Avg"],
                       [[dte, str(cp)[:24], fa.inr(a), fa.inr_c(av)] for dte, cp, a, av in u],
                       aligns=["l", "l", "r", "r"]):
        print(ln)


def cmd_alias(args):
    if args.alias_cmd == "suggest":
        n = fa.alias_suggest(_db(args), args.aliases)
        print(f"Seeded {n} counterparties into {args.aliases}.")
        print("Edit it \u2014 set \"name\" and \"category\" per pattern \u2014 then re-run summary/report/export.")
    else:
        print(f"Alias file: {args.aliases}")
        print("Format: {\"<pattern>\": {\"name\": \"..\", \"category\": \"..\"}} (pattern matched lowercase in counterparty/narration).")
        print("Run `./fold alias suggest` to auto-list your top uncategorized payees.")


# ---- CLI -------------------------------------------------------------------
def add_login_flags(sp):
    sp.add_argument("--method", choices=["qr", "otp"], default=None,
                    help="Login method when auth is needed (default: ask).")
    sp.add_argument("--phone", default=None, help="Phone for --method otp, e.g. +9198XXXXXXXX")
    sp.add_argument("--otp", dest="otp_code", default=None, help="OTP code (non-interactive).")


def add_sync_flags(sp):
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--max-pages", type=int, default=None)
    sp.add_argument("--db", type=Path, default=None, help="sqlite path (default <out>/fold.db)")
    sp.add_argument("--no-db", action="store_true", help="Skip the DB / incremental sync")
    sp.add_argument("--full", action="store_true", help="Re-pull ALL transactions")
    sp.add_argument("--export", action="store_true", help="Rewrite JSON/CSV even if nothing changed")
    sp.add_argument("--ledger", type=Path, default=None, help="Append new txns to a ledger-cli file")
    sp.add_argument("--balance-history", action="store_true")
    sp.add_argument("--login", action="store_true", help="Force a fresh login")
    add_login_flags(sp)


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--secrets", type=Path, default=Path("fold_secrets.json"))
    common.add_argument("--out", type=Path, default=Path("fold_export"))
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("--use-bearer", action="store_true")
    common.add_argument("--aliases", type=Path, default=Path("fold_aliases.json"),
                        help="counterparty alias file (pattern -> {name, category})")

    p = argparse.ArgumentParser(prog="fold", description="Export, sync & summarize your Fold data.")
    sub = p.add_subparsers(dest="cmd")

    ps = sub.add_parser("sync", parents=[common], help="Incremental sync + snapshots (default)")
    add_sync_flags(ps)
    pw = sub.add_parser("watch", parents=[common], help="Sync repeatedly on a schedule")
    pw.add_argument("interval", nargs="?", default="15m", help="e.g. 30s, 5m, 1h (default 15m)")
    add_sync_flags(pw)
    pl = sub.add_parser("login", parents=[common], help="Log in (QR or OTP)")
    add_login_flags(pl)
    pst = sub.add_parser("status", parents=[common], help="Show refresh state (Fold pull vs local sync)")
    pst.add_argument("--db", type=Path, default=None)
    add_login_flags(pst)
    pnw = sub.add_parser("networth", parents=[common], help="Fetch investments + net worth")
    add_login_flags(pnw)
    pm = sub.add_parser("summary", parents=[common], help="Spending insights from the local DB")
    pm.add_argument("--month", default=None, help="YYYY-MM (default: latest month with data)")
    pm.add_argument("--db", type=Path, default=None)
    pr = sub.add_parser("report", parents=[common], help="Generate a local HTML dashboard")
    pr.add_argument("--month", default=None)
    pr.add_argument("--db", type=Path, default=None)
    pr.add_argument("--output", type=Path, default=None)
    pr.add_argument("--open", action="store_true", help="Open the report in your browser")
    pe = sub.add_parser("export", parents=[common], help="Export an Excel workbook (.xlsx)")
    pe.add_argument("--db", type=Path, default=None)
    pe.add_argument("--output", type=Path, default=None)
    pe.add_argument("--lunchmoney", action="store_true", help="Also write a Lunch Money import CSV")
    ptx = sub.add_parser("tax", parents=[common], help="Indian FY (Apr\u2013Mar) income/expense report")
    ptx.add_argument("--fy", default=None, help="e.g. 2025-26 (default: current FY)")
    ptx.add_argument("--db", type=Path, default=None)
    pcm = sub.add_parser("compare", parents=[common], help="Compare two months")
    pcm.add_argument("month1")
    pcm.add_argument("month2")
    pcm.add_argument("--db", type=Path, default=None)
    pun = sub.add_parser("unusual", parents=[common], help="Flag anomalous transactions")
    pun.add_argument("--db", type=Path, default=None)
    pal = sub.add_parser("alias", parents=[common], help="Manage counterparty aliases")
    pal.add_argument("alias_cmd", nargs="?", choices=["suggest"], default=None)
    pal.add_argument("--db", type=Path, default=None)
    return p


def main():
    subcmds = {"sync", "watch", "login", "summary", "status", "report", "export",
               "tax", "compare", "unusual", "alias", "networth"}
    argv = sys.argv[1:]
    if not argv or argv[0] not in subcmds:
        argv = ["sync"] + argv  # default command
    args = build_parser().parse_args(argv)

    try:
        OFFLINE = {"summary": cmd_summary, "report": cmd_report, "export": cmd_export,
                   "tax": cmd_tax, "compare": cmd_compare, "unusual": cmd_unusual, "alias": cmd_alias}
        if args.cmd in OFFLINE:
            OFFLINE[args.cmd](args)
            return

        secrets = load_secrets(args.secrets)
        client = FoldClient(secrets, args.secrets, use_bearer=args.use_bearer,
                            verbose=args.verbose,
                            force_login=getattr(args, "login", False) or args.cmd == "login",
                            login_method=getattr(args, "method", None))
        client.cli_phone = getattr(args, "phone", None)
        client.cli_otp = getattr(args, "otp_code", None)
        client.bootstrap()

        if args.cmd == "login":
            u = (client.fetch_profile().get("user") or {})
            print(f"Logged in as {u.get('first_name','')} {u.get('last_name','')} ({u.get('email','')}).")
            return

        if args.cmd == "status":
            cmd_status(client, args)
            return

        if args.cmd == "networth":
            cmd_networth(client, args)
            return

        db_path = None if args.no_db else (args.db or args.out / "fold.db")
        interval = parse_duration(args.interval) if args.cmd == "watch" else None
        while True:
            do_sync(client, args, db_path, fixed_names=bool(interval))
            if not interval:
                break
            client._log(f"next sync in {args.interval}")
            time.sleep(interval)
    except FoldError as e:
        sys.exit(f"\nERROR: {e}")
    except KeyboardInterrupt:
        sys.exit("\nStopped.")


if __name__ == "__main__":
    main()
