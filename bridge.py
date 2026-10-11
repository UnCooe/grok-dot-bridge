"""One-owner Grok Bot / Dot message bridge. Python 3.11+, standard library only."""
import base64
import hashlib
import hmac
import html
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import ssl
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit


def load_env(path=".env"):
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


def utc(epoch=None):
    return datetime.fromtimestamp(epoch or time.time(), timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


class Rejected(Exception):
    pass


def public_post(url, payload, headers):
    """Resolve once; pin a public IP while preserving hostname TLS validation. No redirects."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise Rejected("HTTPS destination required")
    if parsed.port not in (None, 443):
        raise Rejected("Outbound HTTPS must use port 443")
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses:
        raise Rejected("No destination address")
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not (address.ipv4_mapped if hasattr(address, "ipv4_mapped") and address.ipv4_mapped else address).is_global:
            raise Rejected("Non-public destination blocked")
    family, kind, proto, _, address = addresses[0]
    connection = http.client.HTTPSConnection(parsed.hostname, 443, timeout=10, context=ssl.create_default_context())
    def pinned_connect(*_args, **_kwargs):
        sock = socket.socket(family, kind, proto)
        sock.settimeout(10)
        try:
            sock.connect(address)
            return sock
        except BaseException:
            sock.close()
            raise
    connection._create_connection = pinned_connect
    body = encoded(payload)
    if len(body) > 262144:
        raise Rejected("Payload too large")
    try:
        connection.request("POST", parsed.path + ("?" + parsed.query if parsed.query else ""), body,
                           {"Content-Type": "application/json", **headers})
        response = connection.getresponse()
        return response.status, response.read(262145)
    finally:
        connection.close()


class Bridge:
    def __init__(self, db_path, base_url, admin_key, grok_key, post=public_post):
        self.base = base_url.rstrip("/")
        self.admin_key, self.grok_key, self.post = admin_key, grok_key, post
        self.lock = threading.RLock()
        self.public_limits = {}
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,state TEXT,expires REAL);
          CREATE TABLE IF NOT EXISTS messages(
            id TEXT PRIMARY KEY,conversation_id TEXT,sender TEXT,recipient TEXT,text TEXT,
            reply_to TEXT UNIQUE,sent_at TEXT,received_at TEXT,processed_at TEXT,status TEXT,
            final INTEGER,attempts INTEGER DEFAULT 0,next_attempt REAL DEFAULT 0,
            delivery_url TEXT,subscription_id TEXT,callback_token_hash TEXT,error TEXT);
          CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY,at TEXT,message_id TEXT,stage TEXT,detail TEXT);
          CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,url TEXT,secret TEXT,old_secret TEXT,
            rotate_until REAL,expires REAL,active INTEGER);
          CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY,redirects TEXT);
          CREATE TABLE IF NOT EXISTS codes(hash TEXT PRIMARY KEY,client TEXT,redirect TEXT,challenge TEXT,expires REAL);
          CREATE TABLE IF NOT EXISTS tokens(hash TEXT PRIMARY KEY,client TEXT,kind TEXT,expires REAL);
        """)
        # Never automatically re-trigger a Bot after a process dies during its POST.
        self.db.execute("UPDATE messages SET status='delivery_unknown',error='service restarted during POST' WHERE status='dispatching' AND recipient='grok'")
        self.db.execute("UPDATE messages SET status='queued' WHERE status='dispatching' AND recipient='dot'")

    def audit(self, mid, stage, detail=""):
        self.db.execute("INSERT INTO audit(at,message_id,stage,detail) VALUES(?,?,?,?)", (utc(), mid, stage, detail))

    def message(self, mid):
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        return {k: bool(row[k]) if k == "final" else row[k] for k in ("id", "conversation_id", "sender", "recipient", "text", "reply_to",
                                    "sent_at", "received_at", "processed_at", "status", "final", "attempts", "error")} if row else None

    def expire(self):
        self.db.execute("UPDATE conversations SET state='expired' WHERE state='active' AND expires<?", (time.time(),))
        self.db.execute("UPDATE messages SET status='expired' WHERE status='queued' AND conversation_id IN (SELECT id FROM conversations WHERE state='expired')")

    def send(self, sender, args):
        with self.lock:
            self.expire()
            mid, cid, text = (args.get(k) for k in ("message_id", "conversation_id", "text"))
            if not all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", v) for v in (mid, cid)):
                raise Rejected("Use stable, non-secret message_id and conversation_id, 1..100 safe characters")
            if not isinstance(text, str) or not text.strip() or len(text.encode()) > 16000:
                raise Rejected("text must contain 1..16000 UTF-8 bytes")
            final = args.get("final", False)
            if not isinstance(final, bool):
                raise Rejected("final must be boolean")
            parent_id = args.get("reply_to")
            existing = self.message(mid)
            if existing:
                if (existing["conversation_id"], existing["sender"], existing["text"], existing["reply_to"], bool(existing["final"])) != (cid, sender, text, parent_id, final):
                    raise Rejected("message_id conflict")
                self.audit(mid, "duplicate_suppressed")
                return {**existing, "duplicate": True}
            conv = self.db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            parent = self.message(parent_id) if parent_id else None
            if conv:
                if conv["state"] != "active":
                    raise Rejected("Conversation " + conv["state"])
                last = self.db.execute("SELECT id FROM messages WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1", (cid,)).fetchone()
                if not parent or parent["conversation_id"] != cid or parent["recipient"] != sender or last["id"] != parent_id:
                    raise Rejected("Reply must reference latest message addressed to this sender")
                if parent["received_at"] is None:
                    raise Rejected("claim_message before replying")
                if parent["final"]:
                    raise Rejected("Final message must be completed, not replied to")
                count = self.db.execute("SELECT count(*) FROM messages WHERE conversation_id=?", (cid,)).fetchone()[0]
                if count >= 8:
                    self.audit(parent_id, "round_limit_rejected", mid)
                    raise Rejected("Maximum 4 rounds / 8 alternating messages reached")
            elif parent_id:
                raise Rejected("Unknown conversation")
            recipient = "grok" if sender == "dot" else "dot"
            sub = self.db.execute("SELECT * FROM subscriptions WHERE active=1 AND expires>? ORDER BY rowid DESC LIMIT 1", (time.time(),)).fetchone()
            if not sub:
                raise Rejected("Dot must subscribe before starting a conversation")
            if not os.environ.get("GROK_WEBHOOK_URL") or not os.environ.get("GROK_WEBHOOK_KEY"):
                raise Rejected("Grok webhook is not configured")
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if not conv:
                    self.db.execute("INSERT INTO conversations VALUES(?,?,?)", (cid, "active", time.time() + int(os.getenv("CONVERSATION_TIMEOUT_SECONDS", "900"))))
                self.db.execute("INSERT INTO messages(id,conversation_id,sender,recipient,text,reply_to,sent_at,status,final,delivery_url,subscription_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                                (mid, cid, sender, recipient, text, parent_id, utc(), "queued", int(final), sub["url"], sub["id"]))
                if parent:
                    self.db.execute("UPDATE messages SET processed_at=?,status='replied' WHERE id=?", (utc(), parent_id))
                    self.audit(parent_id, "agent_replied", mid)
                self.audit(mid, "service_received")
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            return self.message(mid)

    def receive(self, sender):
        with self.lock:
            self.expire()
            row = self.db.execute("SELECT m.id FROM messages m JOIN conversations c ON c.id=m.conversation_id WHERE m.recipient=? AND m.received_at IS NULL AND m.status IN ('dispatching','event_accepted') AND c.state='active' ORDER BY m.rowid LIMIT 1", (sender,)).fetchone()
            return self.claim(sender, row[0]) if row else {"claimed": False, "reason": "no_pending_message"}

    def claim(self, sender, mid):
        with self.lock:
            self.expire()
            msg = self.message(mid)
            if not msg or msg["recipient"] != sender:
                raise Rejected("Message not addressed to this agent")
            conv = self.db.execute("SELECT state FROM conversations WHERE id=?", (msg["conversation_id"],)).fetchone()
            if conv["state"] not in ("active", "completed"):
                raise Rejected("Conversation " + conv["state"])
            if msg["received_at"]:
                self.audit(mid, "duplicate_claim_suppressed")
                return {**msg, "claimed": False, "reason": "already_claimed"}
            self.db.execute("UPDATE messages SET received_at=?,status='agent_processing' WHERE id=?", (utc(), mid))
            self.audit(mid, "agent_claimed")
            return {**self.message(mid), "claimed": True}

    def finish(self, sender, mid):
        with self.lock:
            msg = self.message(mid)
            if not msg or msg["recipient"] != sender or not msg["final"] or not msg["received_at"]:
                raise Rejected("Only recipient of a claimed final message can complete it")
            self.db.execute("UPDATE messages SET status='completed',processed_at=? WHERE id=?", (utc(), mid))
            self.db.execute("UPDATE conversations SET state='completed' WHERE id=? AND state='active'", (msg["conversation_id"],))
            self.audit(mid, "agent_completed")
            return self.message(mid)

    def stop(self, cid):
        with self.lock:
            self.db.execute("UPDATE conversations SET state='stopped' WHERE id=? AND state='active'", (cid,))
            self.db.execute("UPDATE messages SET status='stopped' WHERE conversation_id=? AND status='queued'", (cid,))
            self.audit(None, "conversation_stopped", cid)
            return {"conversation_id": cid, "stopped": True}

    def history(self, cid):
        with self.lock:
            self.expire()
            conv = self.db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            ids = self.db.execute("SELECT id FROM messages WHERE conversation_id=? ORDER BY rowid", (cid,)).fetchall()
            audit = self.db.execute("SELECT * FROM audit WHERE message_id IN (SELECT id FROM messages WHERE conversation_id=?) OR (stage='conversation_stopped' AND detail=?) ORDER BY seq", (cid, cid)).fetchall()
            return {"conversation": dict(conv) if conv else None, "messages": [self.message(r[0]) for r in ids], "audit": [dict(r) for r in audit]}

    def event_post(self, sub, payload, eid):
        body = encoded(payload)
        timestamp = str(int(time.time()))
        signatures = []
        keys = [sub["secret"]]
        if sub["old_secret"] and sub["rotate_until"] > time.time():
            keys.append(sub["old_secret"])
        for secret in keys:
            key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
            signature = base64.b64encode(hmac.new(key, eid.encode()+b"."+timestamp.encode()+b"."+body, hashlib.sha256).digest()).decode()
            signatures.append("v1," + signature)
        return self.post(sub["url"], payload, {"webhook-id": eid, "webhook-timestamp": timestamp,
                         "webhook-signature": " ".join(signatures), "X-MCP-Subscription-Id": sub["id"]})

    def subscribe(self, params):
        delivery = params.get("delivery", {})
        if params.get("name") != "message.created" or params.get("arguments") != {"recipient": "dot"} or delivery.get("mode") != "webhook":
            raise Rejected("Subscribe to message.created with recipient=dot and webhook delivery")
        url, secret = delivery.get("url", ""), delivery.get("secret", "")
        callback_host = urlsplit(url).hostname
        if callback_host and re.fullmatch(r"[a-zA-Z0-9.-]{1,253}", callback_host):
            print("subscription callback host", callback_host, flush=True)
        hosts = os.getenv("CALLBACK_HOSTS", "").split(",")
        if callback_host not in hosts:
            print("subscription rejected: callback host not configured", flush=True)
            raise Rejected("Callback host not configured: " + str(urlsplit(url).hostname))
        try:
            if not secret.startswith("whsec_") or not 24 <= len(base64.b64decode(secret[6:], validate=True)) <= 64:
                raise ValueError()
        except (ValueError, TypeError):
            raise Rejected("Invalid signing secret") from None
        sid = "sub_" + digest("dot|" + url + "|message.created|recipient=dot")[:32]
        challenge = secrets.token_urlsafe(32)
        sub = {"id": sid, "url": url, "secret": secret, "old_secret": None, "rotate_until": 0}
        status, response = self.event_post(sub, {"type": "verification", "challenge": challenge}, "verify_" + secrets.token_hex(16))
        try:
            verified = 200 <= status < 300 and hmac.compare_digest(json.loads(response).get("challenge", ""), challenge)
        except (ValueError, TypeError):
            verified = False
        if not verified:
            print("subscription rejected: callback verification failed", status, flush=True)
            raise Rejected("Callback verification failed")
        ttl = params.get("ttlMs", 86400000)
        if ttl is not None and (not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0):
            raise Rejected("Invalid ttlMs")
        expires = time.time() + min(86400, (ttl / 1000 if ttl is not None else 86400))
        with self.lock:
            old = self.db.execute("SELECT secret FROM subscriptions WHERE id=?", (sid,)).fetchone()
            self.db.execute("INSERT OR REPLACE INTO subscriptions VALUES(?,?,?,?,?,?,1)",
                            (sid, url, secret, old[0] if old and old[0] != secret else None, time.time()+300, expires))
            self.audit(None, "dot_subscription_verified", sid)
        return {"id": sid, "refreshBefore": utc(expires), "cursor": None, "truncated": False}

    def dispatch_one(self):
        with self.lock:
            self.expire()
            row = self.db.execute("SELECT * FROM messages WHERE status='queued' AND next_attempt<=? ORDER BY rowid LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return False
            mid = row["id"]
            conv = self.db.execute("SELECT state FROM conversations WHERE id=?", (row["conversation_id"],)).fetchone()
            if conv[0] != "active":
                self.db.execute("UPDATE messages SET status='stopped' WHERE id=?", (mid,))
                return True
            sub = self.db.execute("SELECT * FROM subscriptions WHERE id=? AND active=1 AND expires>?", (row["subscription_id"], time.time())).fetchone()
            if not sub:
                self.db.execute("UPDATE messages SET status='failed',error='Dot subscription unavailable' WHERE id=?", (mid,))
                return True
            self.db.execute("UPDATE messages SET status='dispatching',attempts=attempts+1 WHERE id=?", (mid,))
            self.audit(mid, "dispatch_started")
            message = self.message(mid)
            payload = {k: message[k] for k in ("id", "conversation_id", "sender", "recipient", "text", "reply_to", "sent_at", "final")}
            if row["recipient"] == "grok":
                token = secrets.token_urlsafe(32)
                self.db.execute("UPDATE messages SET callback_token_hash=? WHERE id=?", (digest(token), mid))
                payload["callback_url"] = self.base + "/grok/" + token
                payload["reply_message_id"] = "reply-" + mid
        # No lock across network I/O: Agent callbacks may arrive while POST is pending.
        status, error = None, None
        try:
            if row["recipient"] == "grok":
                status, _ = self.post(os.environ["GROK_WEBHOOK_URL"], payload,
                                      {"Authorization": "Bearer " + os.environ["GROK_WEBHOOK_KEY"]})
            else:
                status, _ = self.event_post(sub, {"eventId": mid, "name": "message.created",
                                                  "timestamp": row["sent_at"], "data": payload, "cursor": None}, mid)
        except (OSError, http.client.HTTPException, Rejected) as exc:
            error = type(exc).__name__
        with self.lock:
            current = self.message(mid)
            if current["status"] != "dispatching":
                self.audit(mid, "delivery_response_after_agent_callback", str(status))
                return True
            if status and 200 <= status < 300:
                state = "routine_accepted" if row["recipient"] == "grok" else "event_accepted"
                self.audit(mid, state, str(status))
            elif row["recipient"] == "dot" and (status is None or status == 429 or status >= 500) and current["attempts"] < 3:
                state = "queued"
            else:
                state = "delivery_unknown" if row["recipient"] == "grok" and status is None else "failed"
                self.audit(mid, state, error or str(status))
            self.db.execute("UPDATE messages SET status=?,error=?,next_attempt=? WHERE id=?",
                            (state, error or (str(status) if state in ("failed", "queued") else None), time.time()+2**current["attempts"], mid))
        return True

    def bearer(self, token):
        if token and hmac.compare_digest(token, self.admin_key):
            return "admin"
        if token and hmac.compare_digest(token, self.grok_key):
            return "grok"
        with self.lock:
            row = self.db.execute("SELECT 1 FROM tokens WHERE hash=? AND kind='access' AND expires>?", (digest(token), time.time())).fetchone()
        return "dot" if row else None

    def allow_public(self, endpoint, limit):
        with self.lock:
            window = int(time.time() // 60)
            old_window, count = self.public_limits.get(endpoint, (window, 0))
            count = count if old_window == window else 0
            self.public_limits[endpoint] = (window, count+1)
            return count < limit


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


STRING = {"type": "string"}
TOOLS = [
    ("send_message", "Send to the other existing agent. Use stable IDs; reply_to must be the latest claimed message. At most 4 rounds (8 messages). final=true requests the recipient to finish without replying.", schema({"conversation_id": STRING, "message_id": STRING, "text": STRING, "reply_to": STRING, "final": {"type": "boolean"}}, ["conversation_id", "message_id", "text"])),
    ("claim_message", "Claim an incoming message before understanding/replying. On event wakeup with missing payload, message_id='' atomically receives the oldest delivered Dot message. If claimed=false, stop. Never poll on a schedule.", schema({"message_id": STRING}, ["message_id"])),
    ("complete_message", "After reading and understanding a final message, complete it without sending another message.", schema({"message_id": STRING}, ["message_id"])),
    ("stop_conversation", "Stop queued delivery and future replies. An already running Agent cannot be forcibly cancelled.", schema({"conversation_id": STRING}, ["conversation_id"])),
    ("get_conversation", "Read messages and stage timestamps. Transport acceptance is not proof an Agent read or understood a message.", schema({"conversation_id": STRING}, ["conversation_id"])),
]
EVENT = {"name": "message.created", "description": "A message from the existing Grok Bot for Dot.", "delivery": ["webhook"],
         "inputSchema": schema({"recipient": {"type": "string", "enum": ["dot"]}}, ["recipient"]),
         "payloadSchema": schema({"id": STRING, "conversation_id": STRING, "sender": STRING, "recipient": STRING,
                                   "text": STRING, "reply_to": {"type": ["string", "null"]}, "sent_at": STRING, "final": {"type": "boolean"}},
                                  ["id", "conversation_id", "sender", "recipient", "text", "reply_to", "sent_at", "final"])}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass  # Paths may contain scoped callback tokens or OAuth codes.

    def respond(self, value, status=200, headers=None, content_type="application/json"):
        body = encoded(value) if content_type == "application/json" else value.encode()
        self.send_response(status)
        for key, val in {"Content-Type": content_type, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                         "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", **(headers or {})}.items():
            self.send_header(key, val)
        self.end_headers()
        self.wfile.write(body)

    @property
    def bridge(self):
        return self.server.bridge

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size < 0 or size > 32768:
            raise Rejected("Request too large")
        return self.rfile.read(size)

    def identity(self):
        auth = self.headers.get("Authorization", "")
        return self.bridge.bearer(auth[7:]) if auth.startswith("Bearer ") else None

    def oauth_params(self, params):
        b = self.bridge
        client = b.db.execute("SELECT redirects FROM clients WHERE id=?", (params.get("client_id"),)).fetchone()
        if not client or params.get("redirect_uri") not in json.loads(client[0]):
            raise Rejected("Invalid client or redirect")
        if params.get("response_type") != "code" or params.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.get("code_challenge", "")):
            raise Rejected("Authorization code + S256 required")
        if params.get("resource") != b.base + "/mcp" or params.get("scope", "bridge") != "bridge":
            raise Rejected("Invalid resource or scope")

    def do_GET(self):
        b, path = self.bridge, urlsplit(self.path).path
        if path.startswith('/.well-known/') or path == '/mcp':
            print('discovery GET', path, flush=True)
        try:
            if path == "/health":
                return self.respond({"service": "grok-dot-bridge", "version": "0.1.0"})
            if path.startswith("/.well-known/oauth-protected-resource"):
                return self.respond({"resource": b.base+"/mcp", "authorization_servers": [b.base], "scopes_supported": ["bridge"]})
            if path == "/.well-known/oauth-authorization-server":
                return self.respond({"issuer": b.base, "authorization_endpoint": b.base+"/authorize", "token_endpoint": b.base+"/token",
                                     "registration_endpoint": b.base+"/register", "response_types_supported": ["code"],
                                     "grant_types_supported": ["authorization_code", "refresh_token"], "token_endpoint_auth_methods_supported": ["none"],
                                     "code_challenge_methods_supported": ["S256"], "scopes_supported": ["bridge"], "authorization_response_iss_parameter_supported": True})
            if path == "/authorize":
                params = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
                with b.lock:
                    self.oauth_params(params)
                fields = "".join('<input type="hidden" name="'+html.escape(k, quote=True)+'" value="'+html.escape(v, quote=True)+'">' for k, v in params.items())
                return self.respond('<!doctype html><title>Connect message bridge</title><h1>Connect your Dot</h1><p>This grants communication access only to your Grok Bot / Dot bridge.</p><form method="post" action="/authorize">'+fields+'<label>Bridge owner key <input type="password" name="owner_key" required autocomplete="off"></label><button>Connect</button></form>',
                                    content_type="text/html; charset=utf-8", headers={"Content-Security-Policy": "default-src 'none'; form-action 'self' https://"+urlsplit(params["redirect_uri"]).hostname+"; frame-ancestors 'none'"})
            if path.startswith("/conversations/") and self.identity() in ("admin", "dot", "grok"):
                return self.respond(b.history(path.rsplit("/", 1)[1]))
            if path == "/mcp":
                if self.identity() != "dot":
                    return self.respond({"error": "Unauthorized"}, 401, {"WWW-Authenticate": 'Bearer resource_metadata="'+b.base+'/.well-known/oauth-protected-resource", scope="bridge"'})
                return self.respond({"error": "Use POST"}, 405)
            return self.respond({"error": "Not found"}, 404)
        except (Rejected, ValueError, TypeError):
            return self.respond({"error": "Invalid request"}, 400)

    def do_POST(self):
        b, path = self.bridge, urlsplit(self.path).path
        self.connection.settimeout(15)
        try:
            raw = self.body()
            if path in ("/authorize", "/token"):
                args = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
            else:
                args = json.loads(raw)
                if not isinstance(args, dict):
                    raise Rejected("JSON object required")
            if path == "/register":
                if not b.allow_public('register', 10):
                    return self.respond({"error": "Rate limited"}, 429)
                redirects = args.get("redirect_uris", [])
                hosts = os.getenv("OAUTH_REDIRECT_HOSTS", "chatgpt.com,chat.openai.com").split(",")
                if not redirects or len(redirects) > 5 or any(urlsplit(u).scheme != "https" or urlsplit(u).hostname not in hosts or urlsplit(u).username or urlsplit(u).fragment for u in redirects):
                    raise Rejected("Redirect URI not allowed")
                client = secrets.token_urlsafe(24)
                with b.lock:
                    b.db.execute("INSERT INTO clients VALUES(?,?)", (client, json.dumps(redirects)))
                return self.respond({"client_id": client, "redirect_uris": redirects, "token_endpoint_auth_method": "none",
                                     "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}, 201)
            if path == "/authorize":
                if not b.allow_public('authorize', 10):
                    return self.respond({"error": "Rate limited"}, 429)
                with b.lock:
                    self.oauth_params(args)
                    if not hmac.compare_digest(args.get("owner_key", ""), b.admin_key):
                        raise Rejected("Owner key invalid")
                    code = secrets.token_urlsafe(32)
                    b.db.execute("INSERT INTO codes VALUES(?,?,?,?,?)", (digest(code), args["client_id"], args["redirect_uri"], args["code_challenge"], time.time()+120))
                query = {"code": code, "state": args.get("state", ""), "iss": b.base}
                return self.respond({}, 303, {"Location": args["redirect_uri"] + ("&" if "?" in args["redirect_uri"] else "?") + urlencode(query)})
            if path == "/token":
                with b.lock:
                    if args.get("resource") != b.base+"/mcp":
                        raise Rejected("Wrong resource")
                    client = args.get("client_id")
                    if args.get("grant_type") == "authorization_code":
                        code = b.db.execute("SELECT * FROM codes WHERE hash=?", (digest(args.get("code", "")),)).fetchone()
                        if not code or code["expires"] < time.time() or code["client"] != client or code["redirect"] != args.get("redirect_uri") or not hmac.compare_digest(code["challenge"], b64(hashlib.sha256(args.get("code_verifier", "").encode()).digest())):
                            raise Rejected("Invalid grant")
                        b.db.execute("DELETE FROM codes WHERE hash=?", (code["hash"],))
                    elif args.get("grant_type") == "refresh_token":
                        token = b.db.execute("SELECT * FROM tokens WHERE hash=? AND kind='refresh' AND expires>?", (digest(args.get("refresh_token", "")), time.time())).fetchone()
                        if not token or token["client"] != client:
                            raise Rejected("Invalid refresh token")
                        b.db.execute("DELETE FROM tokens WHERE hash=?", (token["hash"],))
                    else:
                        raise Rejected("Unsupported grant")
                    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    b.db.executemany("INSERT INTO tokens VALUES(?,?,?,?)", [(digest(access), client, "access", time.time()+3600), (digest(refresh), client, "refresh", time.time()+2592000)])
                return self.respond({"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": 3600, "scope": "bridge"})
            who = self.identity()
            if path == '/mcp':
                method_label = args.get('method', '')
                if isinstance(method_label, str) and re.fullmatch(r'[a-zA-Z/._-]{1,80}', method_label):
                    print('mcp', method_label, 'authenticated' if who else 'unauthenticated', flush=True)
            if path.startswith("/grok/"):
                token_hash = digest(path.rsplit("/", 1)[1])
                with b.lock:
                    msg = b.db.execute("SELECT * FROM messages WHERE callback_token_hash=?", (token_hash,)).fetchone()
                    if not msg:
                        return self.respond({"error": "Unauthorized"}, 401)
                    action = args.get("action")
                    if action == "claim":
                        result = b.claim("grok", msg["id"])
                    elif action == "complete":
                        result = b.finish("grok", msg["id"])
                    elif action == "reply":
                        result = b.send("grok", {"conversation_id": msg["conversation_id"], "message_id": "reply-"+msg["id"], "reply_to": msg["id"], "text": args.get("text"), "final": args.get("final", False)})
                    else:
                        raise Rejected("action must be claim/reply/complete")
                return self.respond(result)
            if not who:
                return self.respond({"error": "Unauthorized"}, 401, {"WWW-Authenticate": 'Bearer resource_metadata="'+b.base+'/.well-known/oauth-protected-resource", scope="bridge"'})
            if path == "/messages" and who in ("dot", "grok"):
                return self.respond(b.send(who, args))
            if path == "/stop" and who in ("admin", "dot", "grok"):
                return self.respond(b.stop(args["conversation_id"]))
            if path != "/mcp" or who != "dot":
                return self.respond({"error": "Forbidden"}, 403)
            request_id = args.get("id")
            method, params = args.get("method"), args.get("params", {})
            try:
                if method == "server/discover":
                    result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}, "events": {}}, "serverInfo": {"name": "grok-dot-bridge", "version": "0.1.0"}}
                elif method == "initialize":
                    result = {"protocolVersion": "2026-07-28", "capabilities": {"tools": {}, "events": {}}, "serverInfo": {"name": "grok-dot-bridge", "version": "0.1.0"}}
                elif method in ("ping", "notifications/initialized"):
                    result = {}
                elif method == "tools/list":
                    result = {"tools": [{"name": name, "description": description, "inputSchema": spec,
                                          "annotations": {"readOnlyHint": name == "get_conversation", "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
                                          "securitySchemes": [{"type": "oauth2", "scopes": ["bridge"]}]} for name, description, spec in TOOLS]}
                elif method == "events/list":
                    result = {"events": [EVENT]}
                elif method == "events/subscribe":
                    result = b.subscribe(params)
                elif method == "events/unsubscribe":
                    if params.get("name") != "message.created" or params.get("arguments") != {"recipient": "dot"}:
                        raise Rejected("Invalid subscription")
                    with b.lock:
                        b.db.execute("UPDATE subscriptions SET active=0 WHERE url=?", (params.get("delivery", {}).get("url"),))
                    result = {}
                elif method == "tools/call":
                    name, arguments = params.get("name"), params.get("arguments", {})
                    spec = next((s for n, _, s in TOOLS if n == name), None)
                    if not spec or not isinstance(arguments, dict) or set(arguments) - set(spec["properties"]) or set(spec["required"]) - set(arguments):
                        raise Rejected("Invalid tool arguments")
                    if name == "send_message":
                        value = b.send("dot", arguments)
                    elif name == "claim_message":
                        value = b.receive("dot") if arguments["message_id"] == "" else b.claim("dot", arguments["message_id"])
                    elif name == "complete_message":
                        value = b.finish("dot", arguments["message_id"])
                    elif name == "stop_conversation":
                        value = b.stop(arguments["conversation_id"])
                    else:
                        value = b.history(arguments["conversation_id"])
                    result = {"content": [{"type": "text", "text": encoded(value).decode()}], "isError": False}
                else:
                    return self.respond({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}})
                if request_id is None:
                    return self.respond({}, 202)
                return self.respond({"jsonrpc": "2.0", "id": request_id, "result": result})
            except (Rejected, OSError, ValueError, TypeError) as exc:
                code = -32015 if method == "events/subscribe" else -32602
                return self.respond({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": str(exc) if isinstance(exc, Rejected) else type(exc).__name__}})
        except (Rejected, KeyError, ValueError, TypeError):
            return self.respond({"error": "Invalid request"}, 400)


def run():
    load_env()
    os.umask(0o077)
    admin, grok = os.getenv("ADMIN_KEY", ""), os.getenv("GROK_KEY", "")
    if len(admin) < 40 or len(grok) < 40 or admin == grok:
        raise SystemExit("Set different random ADMIN_KEY/GROK_KEY (at least 40 characters) in private .env")
    base = os.environ["PUBLIC_BASE_URL"]
    if urlsplit(base).scheme != "https":
        raise SystemExit("PUBLIC_BASE_URL must be HTTPS")
    bridge = Bridge(os.getenv("DB_PATH", "data/bridge.sqlite3"), base, admin, grok)
    server = ThreadingHTTPServer((os.getenv("BIND_HOST", "127.0.0.1"), int(os.getenv("PORT", "8787"))), Handler)
    server.bridge = bridge
    def worker():
        while True:
            try:
                if not bridge.dispatch_one():
                    time.sleep(0.5)
            except Exception as exc:
                # Only exception class: never log URLs, credentials, or response bodies.
                print("delivery_worker:", type(exc).__name__, flush=True)
                time.sleep(1)
    threading.Thread(target=worker, daemon=True).start()
    print("grok-dot-bridge listening", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run()
