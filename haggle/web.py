import hashlib
import hmac
import html
import json
import secrets
import socket
import ssl
import threading
import time
from collections import defaultdict, deque
from email.parser import BytesParser
from email.policy import default
from http import cookies
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from .drafter import DISCLOSURE, DraftRequest
from .fees import Outcome, RATES_BPS, quote, valid_image
from .gate import check, parse_price
from .samples import SAMPLES

BANNER = (
    '<div class="banner" role="note">Demo operated by AI agents. Made-up data only. '
    'Do not enter real personal data. Nothing is charged.</div>'
)
CSS = """body{font:16px system-ui;max-width:760px;margin:auto;padding:1rem;line-height:1.5}.banner{background:#fff3cd;border:1px solid #d6b500;padding:.7rem}.card{border:1px solid #ccc;border-radius:8px;padding:1rem;margin:1rem 0}label{display:block;margin:.7rem 0}input,textarea,select{box-sizing:border-box;max-width:100%;padding:.45rem}textarea{width:100%}button,.button{padding:.55rem .8rem;background:#174ea6;color:white;border:0;border-radius:4px}.disclosure{background:#e8f0fe}.error{color:#9b1c1c}"""
JS = "document.addEventListener('click',async e=>{if(e.target.dataset.copy){let n=document.getElementById(e.target.dataset.copy);try{await navigator.clipboard.writeText(n.innerText);e.target.innerText='Copied!'}catch(_){let r=document.createRange();r.selectNodeContents(n);let s=window.getSelection();s.removeAllRanges();s.addRange(r)}}});"
CSP = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"


def esc(value):
    return html.escape(str(value), quote=True)


def money(cents):
    dollars, remainder = divmod(cents, 100)
    return f"${dollars:,}.{remainder:02d}"


def page(title, body):
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width">'
        f'<title>{esc(title)}</title><link rel="stylesheet" href="/static/app.css"></head>'
        f'<body>{BANNER}<h1>{esc(title)}</h1>{body}'
        '<script src="/static/app.js"></script></body></html>'
    ).encode()


def csrf(token):
    return f'<input type="hidden" name="csrf" value="{esc(token)}">'


def handler_for(app):
    csrf_key = secrets.token_bytes(32)
    rate_lock = threading.Lock()
    rate_buckets = defaultdict(deque)

    def pending_csrf(sid):
        return hmac.new(csrf_key, sid.encode("ascii"), hashlib.sha256).hexdigest()

    def rate_allowed(client):
        now = time.monotonic()
        with rate_lock:
            bucket = rate_buckets[client]
            while bucket and now - bucket[0] >= 60:
                bucket.popleft()
            if len(bucket) >= 120:
                return False
            bucket.append(now)
            if len(rate_buckets) > 2048:
                for key in list(rate_buckets):
                    if not rate_buckets[key] or now - rate_buckets[key][-1] >= 60:
                        rate_buckets.pop(key, None)
            return True

    class Handler(BaseHTTPRequestHandler):
        app_ref = app

        def setup(self):
            super().setup()
            self.request.settimeout(5)

        def log_message(self, *_):
            pass

        def _base_headers(self, cookie=None):
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            if cookie:
                secure = "; Secure" if isinstance(self.connection, ssl.SSLSocket) else ""
                self.send_header(
                    "Set-Cookie",
                    f"hs={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600{secure}",
                )

        def _headers(self, status, mime="text/html; charset=utf-8", length=None, cookie=None):
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self._base_headers(cookie)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.end_headers()

        def _send(self, status, data, mime="text/html; charset=utf-8", cookie=None):
            self._headers(status, mime, len(data), cookie)
            self.wfile.write(data)

        def _redirect(self, path, cookie=None):
            self.send_response(303)
            self.send_header("Location", path)
            self.send_header("Content-Length", "0")
            self._base_headers(cookie)
            self.end_headers()

        def _raw_sid(self):
            try:
                jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
                morsel = jar.get("hs")
                return morsel.value if morsel else ""
            except cookies.CookieError:
                return ""

        @staticmethod
        def _valid_sid(sid):
            return len(sid) == 32 and all(char in "0123456789abcdef" for char in sid)

        def _session(self, pending_ok=False):
            sid = self._raw_sid()
            if self._valid_sid(sid):
                negotiation = self.app_ref.store.get_existing(sid)
                if negotiation is not None:
                    return sid, negotiation, None
                if pending_ok:
                    return sid, None, None
            sid = secrets.token_hex(16)
            return sid, None, sid

        @staticmethod
        def _csrf_value(sid, negotiation):
            return negotiation.csrf if negotiation is not None else pending_csrf(sid)

        def _form(self):
            length = self.headers.get("Content-Length")
            if length is None:
                self._send(411, page("Length required", "<p>A Content-Length header is required.</p>"))
                return None
            try:
                size = int(length)
            except ValueError:
                size = -1
            if size < 0:
                self._send(400, page("Bad request", "<p>Invalid request length.</p>"))
                return None
            if size > 65536:
                self._send(413, page("Request too large", "<p>Request bodies are limited to 64 KB.</p>"))
                return None
            try:
                data = self.rfile.read(size)
            except (socket.timeout, TimeoutError):
                self._send(408, page("Request timeout", "<p>The request body arrived too slowly.</p>"))
                return None
            if len(data) != size:
                self._send(400, page("Bad request", "<p>The request body was incomplete.</p>"))
                return None
            ctype = self.headers.get("Content-Type", "")
            if ctype.startswith("multipart/form-data"):
                try:
                    message = BytesParser(policy=default).parsebytes(
                        (f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n").encode() + data
                    )
                    output = {}
                    for part in message.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        if name:
                            output[name] = part.get_payload(decode=True) if part.get_filename() else part.get_content()
                    return output
                except (TypeError, ValueError, UnicodeError):
                    self._send(400, page("Bad request", "<p>Invalid multipart form.</p>"))
                    return None
            return {
                key: values[-1]
                for key, values in parse_qs(data.decode("utf-8", "replace"), keep_blank_values=True).items()
            }

        def _require_csrf(self, form, sid, negotiation):
            expected = self._csrf_value(sid, negotiation)
            supplied = str(form.get("csrf", "")) if form else ""
            if not supplied or not secrets.compare_digest(supplied, expected):
                self._send(403, page("Forbidden", "<p>Invalid or missing CSRF token.</p>"))
                return False
            return True

        def _make_draft(self, negotiation, discount):
            negotiation.draft = self.app_ref.drafter.draft(
                DraftRequest(
                    negotiation.category,
                    negotiation.asking_cents,
                    negotiation.title,
                    negotiation.text,
                    discount,
                )
            )
            negotiation.marked_sent = False
            negotiation.outcome = None
            negotiation.fee_quote = None
            negotiation.fee_finalized = False
            negotiation.charge_id = ""
            self.app_ref.metrics.incr("draft_created")

        def _reject_finalized_mutation(self, negotiation):
            if negotiation.fee_finalized or negotiation.charge_id:
                self._send(
                    409,
                    page(
                        "Negotiation finalized",
                        "<p>This negotiation has already been finalized. Start over to create a new negotiation.</p>",
                    ),
                )
                return True
            return False

        def do_GET(self):
            try:
                self._do_get()
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass

        def _do_get(self):
            if not rate_allowed(self.client_address[0]):
                return self._send(429, page("Too many requests", "<p>Please wait and try again.</p>"))
            path = urlsplit(self.path).path
            if path == "/healthz":
                return self._send(200, b"ok", "text/plain; charset=utf-8")
            if path == "/static/app.css":
                return self._send(200, CSS.encode(), "text/css; charset=utf-8")
            if path == "/static/app.js":
                return self._send(200, JS.encode(), "text/javascript; charset=utf-8")
            if path == "/metrics":
                if not self.app_ref.show_metrics:
                    return self._send(404, page("Not found", "<p>Page not found.</p>"))
                data = json.dumps(self.app_ref.metrics.snapshot(), sort_keys=True).encode()
                return self._send(200, data, "application/json; charset=utf-8")

            sid, negotiation, new_cookie = self._session()
            token = self._csrf_value(sid, negotiation)
            if path == "/":
                self.app_ref.metrics.incr("visit")
                buttons = "".join(
                    f'<button name="sample_id" value="{key}">Try sample: {esc(value["title"])}</button> '
                    for key, value in list(SAMPLES.items())[:3]
                )
                body = (
                    '<p>Paste a used-goods listing and get a polite offer you send yourself.</p>'
                    f'<form method="post" action="/listing">{csrf(token)}'
                    f'<div class="card">{buttons}</div>'
                    '<label>Optional listing URL <input name="url"></label>'
                    '<label>Title <input name="title" maxlength="120"></label>'
                    '<label>Listing text <textarea name="text" maxlength="4000"></textarea></label>'
                    '<label>Asking price <input name="asking"></label><button>Check listing</button></form>'
                )
                return self._send(200, page("Haggle Agent", body), cookie=new_cookie)
            known_session_paths = {"/terms", "/draft", "/outcome", "/fee", "/done"}
            if path not in known_session_paths:
                return self._send(404, page("Not found", "<p>Page not found.</p>"), cookie=new_cookie)
            if negotiation is None:
                return self._redirect("/", cookie=new_cookie)
            if path == "/terms":
                if not negotiation.asking_cents or not negotiation.category:
                    return self._redirect("/")
                percent = RATES_BPS[negotiation.rate_variant] // 100
                example = money(3000 * RATES_BPS[negotiation.rate_variant] // 10000)
                body = (
                    f'<p>If you save money, we charge {percent}% of the savings, max $50. No savings, no fee.</p>'
                    f'<p>Example: save $30 and the fee is {example}.</p>'
                    f'<form method="post">{csrf(token)}'
                    '<label><input type="checkbox" name="accept" value="yes"> I accept these fee terms</label>'
                    '<p>Demo card ending in 4242; nothing is charged.</p><button>Accept and draft</button></form>'
                )
                return self._send(200, page("Fee terms", body))
            if path == "/draft":
                if not negotiation.terms_accepted:
                    return self._redirect("/terms")
                if negotiation.draft is None:
                    return self._redirect("/terms")
                draft = negotiation.draft
                notice = (
                    '<p class="error">In the pilot, drafts like this would be reviewed before sending.</p>'
                    if draft.requires_review else ""
                )
                messages = (
                    ("opening", "Opening offer", draft.opening),
                    ("counter", "If seller counters", draft.counter_if_countered),
                    ("declined", "If seller declines", draft.counter_if_declined),
                )
                disclosure_html = '<span class="disclosure">' + esc(DISCLOSURE) + "</span>"
                cards = "".join(
                    f'<div class="card"><h2>{label}</h2><p id="{key}">'
                    f'{esc(message).replace(esc(DISCLOSURE), disclosure_html)}</p>'
                    f'<button type="button" data-copy="{key}">Copy</button></div>'
                    for key, label, message in messages
                )
                body = (
                    f'{notice}<form method="post">{csrf(token)}'
                    '<label>Target discount <input type="range" name="discount" min="5" max="30" value="15"></label>'
                    f'<button>Redraft</button></form>{cards}'
                    f'<form method="post" action="/sent">{csrf(token)}<button>I sent it</button></form>'
                )
                return self._send(200, page("Your offer drafts", body))
            if path == "/outcome":
                if not negotiation.marked_sent or negotiation.draft is None:
                    return self._redirect("/draft")
                sample = SAMPLES.get(negotiation.sample_id, {})
                final = sample.get("final", "")
                proof = sample.get("proof", "")
                body = (
                    f'<form method="post" enctype="multipart/form-data">{csrf(token)}'
                    '<label>Seller response <select name="seller_response">'
                    '<option value="countered">Countered</option><option value="accepted">Accepted</option>'
                    '<option value="declined">Declined</option><option value="no_reply">No reply</option></select></label>'
                    f'<label>Final agreed price <input name="final_price" value="{esc(final)}"></label>'
                    f'<label>Proof text <textarea name="proof_text" maxlength="2000">{esc(proof)}</textarea></label>'
                    '<label>Optional PNG or JPEG proof, at most 48 KB <input type="file" name="proof_image" accept="image/png,image/jpeg"></label>'
                    '<button>Calculate fee</button></form>'
                )
                return self._send(200, page("Report outcome", body))
            if path == "/fee":
                if negotiation.outcome is None or negotiation.fee_quote is None:
                    return self._redirect("/outcome")
                result = negotiation.fee_quote
                body = (
                    f'<p>{esc(result.reason)}</p><p>Savings: {money(result.savings_cents)}</p>'
                    f'<p>Fee: {money(result.fee_cents)}</p>'
                    f'<form method="post">{csrf(token)}<button>Finish</button></form>'
                )
                if not result.verified:
                    body += '<p><a href="/outcome">Change the outcome or proof</a></p>'
                return self._send(200, page("Success fee", body))
            if path == "/done":
                if not negotiation.fee_finalized:
                    return self._redirect("/fee")
                charge = esc(negotiation.charge_id) if negotiation.charge_id else "No charge"
                receipt = ""
                if not negotiation.vault_token:
                    receipt = (
                        f'<form method="post">{csrf(token)}'
                        '<label>Optional made-up receipt email <input type="email" name="email" maxlength="254"></label>'
                        '<p>No email is sent. The demo retains only a temporary keyed digest, not the address.</p>'
                        '<button>Save demo receipt preference</button></form>'
                    )
                else:
                    receipt = "<p>Demo receipt preference saved; no email was sent.</p>"
                body = (
                    f'<p>Result: {charge}</p>{receipt}'
                    f'<form method="post" action="/start-over">{csrf(token)}<button>Start over</button></form>'
                )
                return self._send(200, page("Done", body))

        def do_POST(self):
            try:
                self._do_post()
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass

        def _do_post(self):
            if not rate_allowed(self.client_address[0]):
                return self._send(429, page("Too many requests", "<p>Please wait and try again.</p>"))
            path = urlsplit(self.path).path
            sid, negotiation, new_cookie = self._session(pending_ok=path == "/listing")
            form = self._form()
            if form is None:
                return
            if not self._require_csrf(form, sid, negotiation):
                return

            if path == "/listing":
                negotiation = self.app_ref.store.reset(sid, self.app_ref.rate_variant)
                sample_id = str(form.get("sample_id", ""))
                sample = SAMPLES.get(sample_id, {})
                title = str(form.get("title", ""))[:120] or sample.get("title", "")
                text = str(form.get("text", ""))[:4000] or sample.get("text", "")
                asking = str(form.get("asking", "")) or sample.get("asking", "")
                result = check(text, asking, title)
                self.app_ref.metrics.incr("paste")
                if not result.allowed:
                    self.app_ref.metrics.incr("gate_blocked")
                    body = (
                        f'<p>{esc(result.reason)}</p><p>This category is not supported in the demo.</p>'
                        '<p>For safety, submitted text (for example, &lt;script&gt; markup) is not shown.</p>'
                        '<p><a href="/">Try another listing</a></p>'
                    )
                    return self._send(400, page("Listing not supported", body), cookie=new_cookie)
                negotiation.url = str(form.get("url", ""))[:2000]
                negotiation.title = title
                negotiation.text = text
                negotiation.category = result.category or ""
                negotiation.asking_cents = result.asking_cents or 0
                negotiation.sample_id = sample_id
                return self._redirect("/terms", cookie=new_cookie)

            if negotiation is None:
                return self._send(409, page("Out of order", "<p>Start with a supported listing.</p>"), cookie=new_cookie)

            with negotiation.lock:
                if path == "/terms":
                    if self._reject_finalized_mutation(negotiation):
                        return
                    if not negotiation.asking_cents or not negotiation.category:
                        return self._send(409, page("Out of order", "<p>Check a supported listing first.</p>"))
                    if form.get("accept") != "yes":
                        return self._send(400, page("Terms required", "<p>You must accept the fee terms before drafting.</p>"))
                    negotiation.terms_accepted = True
                    negotiation.card_token = self.app_ref.payments.authorize_card_on_file(sid)
                    self.app_ref.metrics.incr("terms_accepted")
                    self._make_draft(negotiation, 15)
                    return self._redirect("/draft")

                if path == "/draft":
                    if self._reject_finalized_mutation(negotiation):
                        return
                    if not negotiation.asking_cents or not negotiation.terms_accepted:
                        return self._send(409, page("Out of order", "<p>Accept the fee terms first.</p>"))
                    try:
                        discount = int(str(form.get("discount", "15")))
                    except ValueError:
                        return self._send(400, page("Bad discount", "<p>Discount must be a whole number from 5 to 30.</p>"))
                    if not 5 <= discount <= 30:
                        return self._send(400, page("Bad discount", "<p>Discount must be a whole number from 5 to 30.</p>"))
                    self._make_draft(negotiation, discount)
                    return self._redirect("/draft")

                if path == "/sent":
                    if self._reject_finalized_mutation(negotiation):
                        return
                    if not negotiation.terms_accepted or negotiation.draft is None:
                        return self._send(409, page("Out of order", "<p>Create a draft before marking it sent.</p>"))
                    negotiation.marked_sent = True
                    negotiation.outcome = None
                    negotiation.fee_quote = None
                    negotiation.fee_finalized = False
                    self.app_ref.metrics.incr("marked_sent")
                    return self._redirect("/outcome")

                if path == "/outcome":
                    if self._reject_finalized_mutation(negotiation):
                        return
                    if not negotiation.marked_sent or negotiation.draft is None:
                        return self._send(409, page("Out of order", "<p>Mark a draft as sent before reporting an outcome.</p>"))
                    response = str(form.get("seller_response", ""))
                    if response not in ("accepted", "countered", "declined", "no_reply"):
                        return self._send(400, page("Bad outcome", "<p>Select a valid seller response.</p>"))
                    final_cents = parse_price(str(form.get("final_price", "")))
                    if response in ("accepted", "countered") and final_cents is None:
                        return self._send(400, page("Bad outcome", "<p>Enter the final agreed price.</p>"))
                    image = form.get("proof_image", b"")
                    if isinstance(image, str):
                        image = image.encode()
                    if image and not valid_image(image):
                        return self._send(400, page("Bad proof image", "<p>Proof must be a PNG or JPEG no larger than 48 KB.</p>"))
                    outcome = Outcome(
                        negotiation.asking_cents,
                        final_cents or 0,
                        response,
                        str(form.get("proof_text", ""))[:2000],
                        len(image),
                        bool(image) and valid_image(image),
                    )
                    result = quote(outcome, RATES_BPS[negotiation.rate_variant])
                    negotiation.outcome = outcome
                    negotiation.fee_quote = result
                    negotiation.fee_finalized = False
                    negotiation.charge_id = ""
                    self.app_ref.metrics.incr("outcome_" + response)
                    self.app_ref.metrics.incr("fee_verified" if result.verified else "fee_unverified")
                    if result.verified and result.fee_cents == 0:
                        self.app_ref.metrics.incr("fee_waived")
                    return self._redirect("/fee")

                if path == "/fee":
                    if not negotiation.marked_sent or negotiation.outcome is None or negotiation.fee_quote is None:
                        return self._send(409, page("Out of order", "<p>Report an outcome before finishing the fee step.</p>"))
                    result = negotiation.fee_quote
                    if result.verified and result.fee_cents > 0 and not negotiation.charge_id:
                        if not negotiation.card_token:
                            return self._send(409, page("Out of order", "<p>No demo card authorization exists.</p>"))
                        outcome = negotiation.outcome
                        material = (
                            f"{sid}|{negotiation.asking_cents}|{outcome.final_cents}|"
                            f"{negotiation.rate_variant}|{result.fee_cents}"
                        )
                        key = "fee_" + hashlib.sha256(material.encode()).hexdigest()
                        charge = self.app_ref.payments.charge(
                            negotiation.card_token,
                            result.fee_cents,
                            key,
                        )
                        negotiation.charge_id = charge.charge_id
                        self.app_ref.metrics.incr("charged")
                    negotiation.fee_finalized = True
                    return self._redirect("/done")

                if path == "/done":
                    if not negotiation.fee_finalized:
                        return self._send(409, page("Out of order", "<p>Finish the fee step first.</p>"))
                    email = str(form.get("email", "")).strip()
                    if email:
                        if "@" not in email or len(email) > 254:
                            return self._send(400, page("Bad email", "<p>Enter a valid made-up email address.</p>"))
                        if not negotiation.vault_token:
                            negotiation.vault_token = self.app_ref.vault.put("email", email)
                    return self._redirect("/done")

                if path == "/start-over":
                    self.app_ref.store.delete(sid)
                    return self._redirect("/")

            return self._send(404, page("Not found", "<p>Page not found.</p>"))

    return Handler
