# quietoffer

Prototype: Hate haggling? quietoffer drafts polite offers for used goods

**Use it free, no account:** https://app-quietoffer.manykiln.com/  
**Try the demo (made-up data):** https://try-quietoffer.manykiln.com/  
**Product page:** https://quietoffer.manykiln.com/

> Built and maintained by AI agents at [Manykiln](https://manykiln.com), a studio whose products are written, tested and operated by AI agents. This is a working prototype, not a finished product. No payment is taken anywhere: any checkout, waitlist or payment code in this project runs against fakes only, and the hosted demo uses made-up data.

- Status: prototype; run the 17 automated tests yourself with `python3 -m unittest discover`
- Runs on the Python 3.12 standard library; run the usable app with `python3 app.py` (see below)
- Licence: MIT. Free to use, fork and improve.
- Issues and pull requests are not monitored yet.

---

# Haggle Agent MVP

Haggle Agent turns a pasted used-goods listing into a polite, AI-disclosed offer, then demonstrates outcome reporting and a capped success fee. This is a user-test demo using made-up data and fake adapters only. It never fetches a listing, sends a marketplace message, calls an LLM, sends email, or charges real money.

## Requirements and running

Python 3.12; nothing to install.

```sh
python3 demo.py
# or
PORT=9000 python3 demo.py
```

Open http://127.0.0.1:8080. The server binds to `127.0.0.1` by default. To expose it deliberately, set `DEMO_HOST` to another address. State is memory-only and disappears on restart.

Run tests with:

```sh
python3 -m unittest discover -s tests -t . -v
```

## Configuration

- `DEMO_HOST`: listen address, default `127.0.0.1`
- `PORT`: listen port, default `8080`
- `PUBLIC_BASE_URL`: public HTTP(S) origin used for accepted Host validation, absolute links, and redirects
- `DEMO_HTTPS_TERMINATED=true`: indicates that TLS is terminated by the hosting proxy and marks session cookies `Secure`
- `HAGGLE_MAX_WORKERS`: maximum request threads, default `16`
- `HAGGLE_RATE_VARIANT`: `r10`, `r15`, or `r20`; default `r20`
- `HAGGLE_SHOW_METRICS=1`: enable aggregate `GET /metrics`; off by default

The studio hosting contract requires no additional flags, files, or confirmation.

## Privacy and safety

Request logging is disabled. Sessions contain no account identity, expire after an hour, and are limited in number. Listing URLs are opaque references and are never fetched. Users copy messages and send them manually.

## Known limitations

The original demo is intentionally memory-only and uses deterministic fake adapters. It remains available through `demo.py` for regression and evaluation.

## Usable app

`quietoffer` is the persistent, self-serve Offer Planner and Haggle Log. Start it with:

```sh
python3 app.py
```

Open http://127.0.0.1:8080. Configuration:

- `APP_HOST`: bind address, default `127.0.0.1`; hosted deployments normally set `0.0.0.0`.
- `PORT`: listen port, default `8080`.
- `PUBLIC_BASE_URL`: accepted public origin, default `https://app-quietoffer.manykiln.com`.
- `APP_HTTPS_TERMINATED=true`: indicates TLS termination by the platform.
- `DATA_DIR`: the only writable location, default `./data`.
- `APP_DATA_CAP_MB`: total storage cap in megabytes, default `200`.

Starting a workspace creates a private random link. Its token is the only key: bookmark it, because there are no accounts and a lost link cannot be recovered. The raw token is never stored; its SHA-256 digest names the workspace directory. Data is written atomically and survives restarts. Each workspace can hold up to 200 items, each item up to 100 log entries, and at most 5 MB. Requests and imports are limited to 1 MB. The service supports at most 3,000 workspaces and removes workspaces idle for 90 days; empty workspaces expire after 24 hours. Export important data as JSON before it is needed elsewhere.

The usable app makes no AI or LLM calls. Offers and messages are calculated with fixed rounding rules and fixed templates, and the user copies and sends every message. The old AI disclosure is therefore not shown: claiming AI drafted these messages would be false. An optional, accurate note says that a template tool drafted the message and the user sent it.

Suggestions are rule-based, nothing synchronizes with marketplaces, listing URLs are labels and are never fetched, and the app cannot recover a lost workspace link. The app does not send messages, collect contact details, process payments, or run analytics.
