import html
import json
import secrets
import socket
import threading
import time
from collections import deque
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from .gate import parse_price
from .planner import friendly, money, next_counter, plan_item, scrub, summary
from .samples import SAMPLES
from .wstore import MAX_ITEMS, MAX_LOGS, StoreError

CSP = "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
NOTICE = "Operated by AI agents. Early version: keep your own copy of anything important. Do not enter sellers' names, phone numbers, addresses or other people's personal data, or payment card details. Nothing is charged. Empty workspaces expire after 24 hours; other workspaces expire 90 days after their last view or change."
HONESTY = "Offers and messages come from fixed rules and templates. No AI model is used. You send every message yourself."
CSS = "body{font:16px system-ui;max-width:920px;margin:auto;padding:1rem;line-height:1.5;color:#17212b}h1{margin-bottom:.25rem}.notice{background:#fff4ce;border:1px solid #d2a600;padding:.8rem}.honesty{background:#e9f2ff;padding:.8rem}.card{border:1px solid #ccd3db;border-radius:10px;padding:1rem;margin:1rem 0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:.7rem}label{display:block;margin:.8rem 0}input,textarea,select{box-sizing:border-box;max-width:100%;padding:.5rem}input[type=text],textarea{width:100%}button,.button{display:inline-block;background:#185abc;color:#fff;border:0;border-radius:5px;padding:.6rem .85rem;text-decoration:none;cursor:pointer}.danger{background:#a52020}.error{color:#9b1c1c}.muted{color:#58636f}"
JS = "document.addEventListener('click',async e=>{let id=e.target.dataset.copy;if(!id)return;let n=document.getElementById(id);try{await navigator.clipboard.writeText(n.innerText);e.target.innerText='Copied!'}catch(_){let r=document.createRange();r.selectNodeContents(n);let s=getSelection();s.removeAllRanges();s.addRange(r)}});document.addEventListener('input',e=>{if(e.target.matches('[data-output]')){let n=document.getElementById(e.target.dataset.output);if(n)n.value=e.target.value}});document.addEventListener('submit',e=>{let m=e.target.dataset.confirm;if(m&&!confirm(m))e.preventDefault()});"


class RequestError(Exception):
    def __init__(self, status, title, message):
        super().__init__(message)
        self.status, self.title = status, title


def esc(value):
    return html.escape(str(value), quote=True)


def hosting_settings(public_base_url, listen_host, secure):
    parsed = urlsplit(public_base_url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("PUBLIC_BASE_URL must be an http(s) origin")
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    hosts = {"127.0.0.1", "localhost", "::1", parsed.hostname.lower()}
    if listen_host not in ("", "0.0.0.0", "::"):
        hosts.add(listen_host.lower())
    return {"origin": origin, "hosts": frozenset(hosts), "secure": secure}


def handler_for(store, settings):
    budget_lock = threading.Lock()
    emergency_gets, emergency_posts, new_workspaces = deque(), deque(), deque()
    workspace_gets, workspace_posts = {}, {}
    budget_calls = 0

    def budget(bucket, seconds, limit):
        now = time.monotonic()
        with budget_lock:
            while bucket and now - bucket[0] >= seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True

    def workspace_budget(table, key, seconds, limit):
        nonlocal budget_calls
        now = time.monotonic()
        with budget_lock:
            budget_calls += 1
            if budget_calls % 256 == 0:
                for old_key, old_bucket in list(table.items()):
                    while old_bucket and now - old_bucket[0] >= seconds:
                        old_bucket.popleft()
                    if not old_bucket:
                        table.pop(old_key, None)
            bucket = table.setdefault(key, deque())
            while bucket and now - bucket[0] >= seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.request.settimeout(1)

        def log_message(self, *_):
            pass

        def _security_headers(self):
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Cache-Control", "no-store")

        def _headers(self, status, mime, length, disposition=None):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(length))
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self._security_headers()
            self.end_headers()

        def _send(self, status, data, mime="text/html; charset=utf-8"):
            if isinstance(data, str):
                data = data.encode("utf-8")
            self._headers(status, mime, len(data))
            self.wfile.write(data)

        def _redirect(self, path):
            self.send_response(303)
            self.send_header("Location", path)
            self.send_header("Content-Length", "0")
            self._security_headers()
            self.end_headers()

        def _page(self, title, body):
            return "<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width'><title>" + esc(title) + " · quietoffer</title><link rel=stylesheet href=/static/app.css></head><body><div class=notice role=note>" + esc(NOTICE) + " <a href='https://manykiln.com/privacy' rel=noreferrer>Privacy</a></div><p class=honesty>" + esc(HONESTY) + "</p><h1>" + esc(title) + "</h1>" + body + "<script src=/static/app.js></script></body></html>"

        def _error(self, status, title, message):
            self._send(status, self._page(title, "<p class=error>" + esc(message) + "</p>"))

        def _host_ok(self):
            try:
                host = urlsplit("//" + self.headers.get("Host", "")).hostname
            except ValueError:
                host = None
            return bool(host and host.lower() in settings["hosts"])

        def _origin_ok(self):
            origin = self.headers.get("Origin")
            if origin is not None:
                return origin == settings["origin"]
            referer = self.headers.get("Referer", "")
            return referer == settings["origin"] or referer.startswith(settings["origin"] + "/")

        def _body(self):
            raw = self.headers.get("Content-Length")
            if raw is None:
                self._error(411, "Length required", "A Content-Length header is required.")
                return None
            try:
                size = int(raw)
            except ValueError:
                size = -1
            if size < 0:
                self._error(400, "Bad request", "Invalid request length.")
                return None
            if size > 1024 * 1024:
                self._error(413, "Request too large", "Requests are limited to 1 MB.")
                return None
            deadline = time.monotonic() + 5
            chunks, remaining = [], size
            try:
                while remaining:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise socket.timeout
                    self.request.settimeout(min(1, left))
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
            except (socket.timeout, TimeoutError):
                self._error(408, "Request timeout", "The request body arrived too slowly.")
                return None
            data = b"".join(chunks)
            if len(data) != size:
                self._error(400, "Bad request", "The request body was incomplete.")
                return None
            content_type = self.headers.get("Content-Type", "")
            if content_type.startswith("multipart/form-data"):
                try:
                    message = BytesParser(policy=default).parsebytes((f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode() + data)
                    result = {}
                    for part in message.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        if name:
                            result[name] = part.get_payload(decode=True) if part.get_filename() else part.get_content()
                    return result
                except (TypeError, ValueError, UnicodeError):
                    self._error(400, "Bad request", "Invalid upload.")
                    return None
            return {key: values[-1] for key, values in parse_qs(data.decode("utf-8", "replace"), keep_blank_values=True).items()}

        @staticmethod
        def _parts(path):
            return [part for part in path.split("/") if part]

        @staticmethod
        def _time(ts):
            try:
                return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(ts)))
            except (OverflowError, OSError, TypeError, ValueError):
                return "Unknown date"

        def _dashboard(self, token, data, extra=""):
            totals = summary(data["items"])
            cards = "".join(f"<div class=card><a href='/w/{esc(token)}/i/{esc(item['id'])}'><strong>{esc(item['title'] or 'Untitled item')}</strong></a><br>{money(item['asking_cents'])} · {esc(item['category'])} · {esc(item['status'])}</div>" for item in data["items"])
            body = "<p><strong>Bookmark this link. It is the only key and cannot be recovered.</strong></p>" + extra
            body += "<div class=grid><div class=card>In progress<br><strong>" + str(totals["open"]) + "</strong></div><div class=card>Deals closed<br><strong>" + str(totals["closed"]) + "</strong></div><div class=card>Total asked<br><strong>" + money(totals["asked"]) + "</strong></div><div class=card>Total paid<br><strong>" + money(totals["paid"]) + "</strong></div><div class=card>Total saved<br><strong>" + money(totals["saved"]) + "</strong></div></div><h2>Items</h2>" + (cards or "<p class=muted>No items yet.</p>")
            body += f"<h2>Add item</h2><form method=post action='/w/{esc(token)}/items'><label>Title (up to 120 characters)<input type=text name=title maxlength=120 required></label><label>Listing text<textarea name=notes maxlength=4000 required></textarea></label><label>Asking price<input type=text name=asking required placeholder='$1,234.56, 1234 or 1.2k'></label><label>Optional listing reference label<input type=text name=url_label maxlength=300></label><label>Target discount <input type=range name=discount min=5 max=30 value=15 data-output=discount-output><output id=discount-output>15</output>%</label><label><input type=checkbox name=template_note value=yes> Add the template-tool note</label><button>Add item</button></form>"
            body += f"<h2>Workspace data</h2><p><a class=button href='/w/{esc(token)}/export'>Export everything</a></p><form method=post enctype=multipart/form-data action='/w/{esc(token)}/import'><label>Import JSON (up to 1 MB)<input type=file name=import accept='application/json' required></label><button>Import JSON</button></form><form method=post action='/w/{esc(token)}/delete' data-confirm='Delete this workspace permanently?'><button class=danger>Delete this workspace now</button></form>"
            return self._page("Your quietoffer workspace", body)

        def _item_page(self, token, item):
            meet = friendly((item["offer_cents"] + item["walkaway_cents"]) // 2)
            cards = ""
            for key, label in (("opening", "Opening message"), ("countered", "If the seller counters"), ("declined", "If the seller declines")):
                cards += f"<div class=card><h2>{label}</h2><p id='msg-{key}'>{esc(item['messages'][key])}</p><button type=button data-copy='msg-{key}'>Copy</button></div>"
            last_offer, latest = item["offer_cents"], None
            for entry in item["log"]:
                if entry.get("kind") == "sent" and int(entry.get("price_cents") or 0) > 0:
                    last_offer = int(entry["price_cents"])
                elif entry.get("kind") == "countered" and int(entry.get("price_cents") or 0) > 0:
                    latest = (last_offer, int(entry["price_cents"]))
            suggestion = ""
            if latest:
                value = next_counter(latest[0], item["walkaway_cents"], latest[1])
                suggestion = "<div class=card><h2>Suggested next step</h2>"
                if value == "accept":
                    suggestion += "<p>The seller's counter is at or below your walk-away ceiling. Consider accepting.</p>"
                else:
                    reply = f"Thanks! Could we meet at {money(value)}? I can pick up quickly."
                    suggestion += f"<p>Suggested counter: <strong>{money(value)}</strong></p><p id=next-reply>{esc(reply)}</p><button type=button data-copy=next-reply>Copy</button>"
                suggestion += "</div>"
            timeline = "".join(f"<li><time>{esc(self._time(entry.get('ts')))}</time> — {esc(entry.get('kind', '').replace('_', ' '))}{' · ' + money(entry['price_cents']) if entry.get('price_cents') else ''}{' · ' + esc(entry['note']) if entry.get('note') else ''}</li>" for entry in item["log"])
            reference = f"<p>Listing reference: {esc(item['url_label'])}</p>" if item.get("url_label") else ""
            body = f"<p><a href='/w/{esc(token)}'>← Workspace</a></p>{reference}<div class=grid><div class=card>Opening offer<br><strong>{money(item['offer_cents'])}</strong></div><div class=card>Meet in the middle<br><strong>{money(meet)}</strong></div><div class=card>Walk-away ceiling<br><strong>{money(item['walkaway_cents'])}</strong></div><div class=card>Asking price<br><strong>{money(item['asking_cents'])}</strong></div></div>{cards}{suggestion}"
            body += f"<h2>Redraft</h2><form method=post action='/w/{esc(token)}/i/{item['id']}/redraft'><label>Target discount <input type=range name=discount min=5 max=30 value='{item['discount']}'></label><button>Redraft</button></form><h2>Negotiation log</h2><ol>{timeline or '<li>No entries yet.</li>'}</ol>"
            body += f"<h2>Add log entry</h2><form method=post action='/w/{esc(token)}/i/{item['id']}/log'><label>What happened?<select name=kind><option value=sent>Offer sent</option><option value=countered>Seller countered</option><option value=accepted>Accepted</option><option value=declined>Declined</option><option value=no_reply>No reply</option><option value=closed_no_deal>Closed without a deal</option></select></label><label>Offer, seller counter or final agreed price<input type=text name=price></label><label>Private note<textarea name=note maxlength=500></textarea></label><button>Add entry</button></form><form method=post action='/w/{esc(token)}/i/{item['id']}/delete' data-confirm='Delete this item?'><button class=danger>Delete item</button></form>"
            return self._page(item["title"] or "Item", body)

        def do_GET(self):
            try:
                if not self._host_ok():
                    return self._send(421, b"Misdirected Request", "text/plain; charset=utf-8")
                path = urlsplit(self.path).path
                if path == "/healthz":
                    return self._send(200, b"ok", "text/plain; charset=utf-8")
                if path == "/static/app.css":
                    return self._send(200, CSS, "text/css; charset=utf-8")
                if path == "/static/app.js":
                    return self._send(200, JS, "text/javascript; charset=utf-8")
                if not budget(emergency_gets, 60, 12000):
                    return self._error(429, "Too many requests", "Please wait and try again.")
                if path == "/":
                    body = "<p>Plan a fair offer, copy polite messages, and keep a private negotiation log.</p><form method=post action=/new><button>Start a workspace</button></form><form method=post action=/new-sample><button>Try with sample data</button></form>"
                    return self._send(200, self._page("quietoffer", body))
                parts = self._parts(path)
                if len(parts) >= 2 and parts[0] == "w" and store.valid_token(parts[1]):
                    token = parts[1]
                    if not store.exists(token):
                        return self._error(404, "Not found", "Page not found.")
                    if not workspace_budget(workspace_gets, store.digest(token), 60, 240):
                        return self._error(429, "Too many requests", "Please wait and try again.")
                    data = store.read(token)
                    if data is None:
                        return self._error(404, "Not found", "Page not found.")
                    if len(parts) == 2:
                        return self._send(200, self._dashboard(token, data))
                    if len(parts) == 3 and parts[2] == "export":
                        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                        self._headers(200, "application/json; charset=utf-8", len(payload), "attachment; filename=quietoffer-export.json")
                        return self.wfile.write(payload)
                    if len(parts) == 4 and parts[2] == "i":
                        item = next((value for value in data["items"] if value["id"] == parts[3]), None)
                        if item is None:
                            return self._error(404, "Not found", "Page not found.")
                        return self._send(200, self._item_page(token, item))
                return self._error(404, "Not found", "Page not found.")
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass

        def do_POST(self):
            try:
                if not self._host_ok():
                    return self._send(421, b"Misdirected Request", "text/plain; charset=utf-8")
                if not self._origin_ok():
                    return self._error(403, "Forbidden", "This form must be submitted from quietoffer.")
                if not budget(emergency_posts, 60, 10000):
                    return self._error(429, "Too many requests", "Please wait and try again.")
                path = urlsplit(self.path).path
                if path in ("/new", "/new-sample"):
                    if not budget(new_workspaces, 3600, 300):
                        return self._error(429, "Too many workspaces", "Please wait before creating another workspace.")
                    if self._body() is None:
                        return
                    token = store.create()
                    if path == "/new-sample":
                        def add_samples(data):
                            for sample in list(SAMPLES.values())[:3]:
                                planned = plan_item(sample["title"], sample["text"], sample["asking"], 15, False)
                                planned.update({"id": secrets.token_hex(4), "url_label": "", "log": [], "status": "in_progress", "final_cents": None})
                                data["items"].append(planned)
                        store.update(token, add_samples)
                    return self._redirect("/w/" + token)
                parts = self._parts(path)
                if len(parts) < 3 or parts[0] != "w" or not store.valid_token(parts[1]):
                    return self._error(404, "Not found", "Page not found.")
                token = parts[1]
                if not store.exists(token):
                    return self._error(404, "Not found", "Page not found.")
                if not workspace_budget(workspace_posts, store.digest(token), 60, 120):
                    return self._error(429, "Too many changes", "Please wait and try again.")
                form = self._body()
                if form is None:
                    return
                if len(parts) == 3 and parts[2] == "delete":
                    store.delete(token)
                    return self._redirect("/")
                if len(parts) == 3 and parts[2] == "items":
                    try:
                        planned = plan_item(str(form.get("title", ""))[:120], str(form.get("notes", ""))[:4000], str(form.get("asking", "")), form.get("discount", 15), form.get("template_note") == "yes")
                    except ValueError as error:
                        return self._error(400, "Could not add item", str(error))
                    planned.update({"id": secrets.token_hex(4), "url_label": scrub(form.get("url_label", ""))[:300], "log": [], "status": "in_progress", "final_cents": None})
                    def add(data):
                        if len(data["items"]) >= MAX_ITEMS:
                            raise StoreError("A workspace can hold at most 200 items.")
                        data["items"].append(planned)
                    store.update(token, add)
                    return self._redirect(f"/w/{token}/i/{planned['id']}")
                if len(parts) == 3 and parts[2] == "import":
                    uploaded = form.get("import", b"")
                    if isinstance(uploaded, str):
                        uploaded = uploaded.encode("utf-8")
                    if len(uploaded) > 1024 * 1024:
                        raise StoreError("Imports are limited to 1 MB.")
                    try:
                        incoming = json.loads(uploaded.decode("utf-8"))
                        source_items = incoming["items"]
                        if incoming.get("version") != 1 or not isinstance(source_items, list) or not all(isinstance(item, dict) for item in source_items):
                            raise ValueError
                    except (ValueError, TypeError, KeyError, UnicodeError):
                        return self._error(400, "Invalid import", "Choose a JSON file exported by quietoffer.")
                    deadline = time.monotonic() + 3
                    def import_items(data):
                        added = 0
                        used = {item.get("id") for item in data["items"]}
                        now = int(time.time())
                        for source in source_items[:MAX_ITEMS]:
                            if len(data["items"]) >= MAX_ITEMS or time.monotonic() >= deadline:
                                break
                            item_id = source.get("id", "")
                            valid_id = isinstance(item_id, str) and len(item_id) == 8 and all(c in "0123456789abcdef" for c in item_id)
                            if valid_id and item_id in used:
                                continue
                            try:
                                cents = int(source["asking_cents"])
                                if cents <= 0:
                                    raise ValueError
                                asking = f"{cents // 100}.{cents % 100:02d}"
                                planned = plan_item(scrub(source.get("title", ""))[:120], scrub(source.get("notes", ""))[:4000], asking, source.get("discount", 15), bool(source.get("template_note")))
                            except (ValueError, TypeError, KeyError):
                                continue
                            if not valid_id:
                                item_id = secrets.token_hex(4)
                                while item_id in used:
                                    item_id = secrets.token_hex(4)
                            logs = []
                            raw_logs = source.get("log", [])
                            if isinstance(raw_logs, list):
                                for entry in raw_logs[:MAX_LOGS]:
                                    if not isinstance(entry, dict) or entry.get("kind") not in ("sent", "countered", "accepted", "declined", "no_reply", "closed_no_deal"):
                                        continue
                                    try:
                                        stamp = int(entry.get("ts", now))
                                    except (TypeError, ValueError, OverflowError):
                                        stamp = now
                                    if stamp < 0 or stamp > now + 86400:
                                        stamp = now
                                    try:
                                        price = int(entry.get("price_cents") or 0)
                                    except (TypeError, ValueError, OverflowError):
                                        price = 0
                                    logs.append({"ts": stamp, "kind": entry["kind"], "price_cents": price if price > 0 else None, "note": scrub(entry.get("note", ""))[:500]})
                            try:
                                final = int(source.get("final_cents") or 0)
                            except (TypeError, ValueError, OverflowError):
                                final = 0
                            final = final if 0 < final <= planned["asking_cents"] else None
                            status = "closed" if final else "closed_no_deal" if source.get("status") == "closed_no_deal" else "in_progress"
                            planned.update({"id": item_id, "url_label": scrub(source.get("url_label", ""))[:300], "log": logs, "status": status, "final_cents": final})
                            data["items"].append(planned)
                            used.add(item_id)
                            added += 1
                        return added, data
                    added, data = store.update(token, import_items)
                    return self._send(200, self._dashboard(token, data, f"<p>Imported {added} item(s).</p>"))
                if len(parts) == 5 and parts[2] == "i":
                    item_id, action = parts[3], parts[4]
                    def change(data):
                        item = next((value for value in data["items"] if value.get("id") == item_id), None)
                        if item is None:
                            raise RequestError(404, "Not found", "Page not found.")
                        if action == "delete":
                            data["items"].remove(item)
                            return
                        if action == "redraft":
                            try:
                                planned = plan_item(item["title"], item["notes"], f"{item['asking_cents'] // 100}.{item['asking_cents'] % 100:02d}", form.get("discount", 15), item.get("template_note", False))
                            except ValueError as error:
                                raise RequestError(400, "Bad discount", str(error)) from error
                            for key in ("category", "discount", "offer_cents", "walkaway_cents", "messages"):
                                item[key] = planned[key]
                            return
                        if action != "log":
                            raise RequestError(404, "Not found", "Page not found.")
                        if len(item["log"]) >= MAX_LOGS:
                            raise StoreError("An item can hold at most 100 log entries.")
                        kind = str(form.get("kind", ""))
                        if kind not in ("sent", "countered", "accepted", "declined", "no_reply", "closed_no_deal"):
                            raise RequestError(400, "Bad log entry", "Select a valid response.")
                        price = parse_price(str(form.get("price", "")))
                        if kind == "countered" and (price is None or price <= 0):
                            raise RequestError(400, "Bad counter", "Enter the seller's counter price.")
                        if kind == "accepted" and (price is None or price <= 0 or price > item["asking_cents"]):
                            raise RequestError(400, "Bad final price", "The final price must be greater than zero and no more than the asking price.")
                        if kind == "sent" and price is not None and (price <= 0 or price > item["asking_cents"]):
                            raise RequestError(400, "Bad offer", "The offer must be greater than zero and no more than the asking price.")
                        stored_price = price if kind in ("sent", "countered", "accepted") else None
                        item["log"].append({"ts": int(time.time()), "kind": kind, "price_cents": stored_price, "note": scrub(form.get("note", ""))[:500]})
                        if kind == "accepted":
                            item["final_cents"], item["status"] = price, "closed"
                        elif kind == "closed_no_deal":
                            item["final_cents"], item["status"] = None, "closed_no_deal"
                    store.update(token, change)
                    destination = f"/w/{token}" if action == "delete" else f"/w/{token}/i/{item_id}"
                    return self._redirect(destination)
                return self._error(404, "Not found", "Page not found.")
            except RequestError as error:
                self._error(error.status, error.title, str(error))
            except StoreError as error:
                self._error(error.status, "Unable to save", str(error))
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass

    return Handler
