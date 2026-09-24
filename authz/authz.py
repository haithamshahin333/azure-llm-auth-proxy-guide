"""ext_authz sidecar for the LangSmith LLM auth proxy (Python standard library only).

Envoy has already verified the LangSmith-signed JWT (signature, issuer, audience) before it calls this sidecar. For
every request the sidecar returns the upstream Authorization header for Azure AI Foundry and prints one JSON line
saying who asked. Token requests are answered only when they come from localhost, i.e. from Envoy in the same pod;
/healthz stays open for the kubelet's readiness probe. How the token is obtained is inferred from the environment,
in this order:
  AZURE_FEDERATED_TOKEN_FILE set   AKS workload identity: the projected token is sent as a client_assertion for
                                   AZURE_CLIENT_ID to AZURE_AUTHORITY_HOST/AZURE_TENANT_ID (all injected by the webhook)
  OAUTH_CLIENT_SECRET set          OAuth2 client_credentials with a client secret: OAUTH_TOKEN_URL, OAUTH_CLIENT_ID,
                                   OAUTH_SCOPE, OAUTH_CLIENT_AUTH (post | basic)
GET /healthz answers 200 only when a token can be minted: the readiness probe, so `helm install --wait` fails when the
identity, federated credential, role or client secret is wrong.
Every other request is logged as one JSON line: method, original path, the host LangSmith addressed, Envoy's request id,
whether the token came from the cache, the JWT claims (organization, workspace, user, actor type) and, by default, every
header Envoy forwarded with credentials redacted (LOG_HEADERS=false for the compact form). Requests Envoy refuses itself
(401 bad or missing token, 403 wrong audience) never reach this sidecar and are only visible to the caller.
"""
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
DEFAULT_SCOPE = "https://cognitiveservices.azure.com/.default"
LOGGED = ("organization_id", "organization_name", "workspace_id", "workspace_name", "ls_user_id", "actor_type", "sub", "request_id")
REDACT = ("x-langsmith-llm-auth", "authorization", "api-key", "x-api-key", "cookie", "proxy-authorization")
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")  # only Envoy, in the same pod, may ask for a token
LOG_HEADERS = os.environ.get("LOG_HEADERS", "true").lower() not in ("0", "false", "no")  # on by default


def log(**fields):
    print(json.dumps(fields), flush=True)


class Credential:
    """OAuth2 client_credentials grant (RFC 6749 section 4.4). One cached token per process, refreshed two minutes
    before it expires, one token request at a time. auth: assertion (workload identity), post or basic."""

    def __init__(self, url, client_id, scope, auth, secret=None, assertion_file=None):
        self.url, self.client_id, self.scope, self.auth = url, client_id, scope, auth
        self._secret, self._file = secret, assertion_file
        self._lock, self._token, self._exp = threading.Lock(), None, 0.0

    def get(self):  # -> (token, "cache" | "minted" | "stale"); "stale" = refresh failed, cached token still valid
        with self._lock:
            now = time.time()
            if self._token and self._exp - now > 120:
                return self._token, "cache"
            try:
                t0 = time.time()
                self._token, self._exp = self._fetch()
                log(msg="token minted", mint_ms=int((time.time() - t0) * 1000), expires_in=int(self._exp - time.time()))
                return self._token, "minted"
            except Exception as exc:
                if self._token and self._exp > now + 5:  # early refresh failed: the cached token is still valid
                    log(warning=f"token refresh failed, serving the cached token: {exc}")
                    return self._token, "stale"
                raise

    def _fetch(self):
        body = {"grant_type": "client_credentials", "client_id": self.client_id}
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        if self.scope:
            body["scope"] = self.scope
        if self.auth == "assertion":
            with open(self._file) as f:  # read every time: the projected token rotates
                body.update(client_assertion_type=ASSERTION_TYPE, client_assertion=f.read().strip())
        elif self.auth == "basic":  # client_secret_basic
            del body["client_id"]
            headers["Authorization"] = "Basic " + base64.b64encode(f"{self.client_id}:{self._secret}".encode()).decode()
        else:  # client_secret_post, what Entra expects by default
            body["client_secret"] = self._secret
        req = urllib.request.Request(self.url, urllib.parse.urlencode(body).encode(), headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                j = json.load(r)
        except urllib.error.HTTPError as e:  # surface the OAuth error code, never the raw body
            try:
                err = json.loads(e.read().decode(errors="replace"))
                detail = f"{err.get('error')}: {str(err.get('error_description', ''))[:200]}"
            except (ValueError, AttributeError):
                detail = "non-JSON error body"
            raise RuntimeError(f"token endpoint HTTP {e.code} ({detail})") from None
        return j["access_token"], time.time() + int(j.get("expires_in", 3600))


def make_credential():  # -> (mode, Credential); exits with a clear message when the environment is incomplete
    env = os.environ.get
    if env("AZURE_FEDERATED_TOKEN_FILE"):  # path A: the workload identity webhook injected the AZURE_* variables
        tenant = env("AZURE_TENANT_ID")
        if not tenant:
            sys.exit("AZURE_TENANT_ID is empty: is the pod labeled azure.workload.identity/use=true?")
        url = env("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com/").rstrip("/") + f"/{tenant}/oauth2/v2.0/token"
        cred = Credential(url, env("AZURE_CLIENT_ID"), env("TOKEN_SCOPE") or DEFAULT_SCOPE, "assertion",
                          assertion_file=env("AZURE_FEDERATED_TOKEN_FILE"))
        return "workload-identity", cred
    if env("OAUTH_CLIENT_SECRET"):  # path B: client id + secret from the values file and the Kubernetes Secret
        url, client_id, auth = env("OAUTH_TOKEN_URL"), env("OAUTH_CLIENT_ID"), env("OAUTH_CLIENT_AUTH", "post")
        if not url:
            sys.exit("OAUTH_TOKEN_URL is empty")
        if not client_id:
            sys.exit("OAUTH_CLIENT_ID is empty")
        if auth not in ("post", "basic"):
            sys.exit(f"OAUTH_CLIENT_AUTH must be post or basic (got {auth!r})")
        return "client-credentials", Credential(url, client_id, env("OAUTH_SCOPE", ""), auth, secret=env("OAUTH_CLIENT_SECRET"))
    sys.exit("no credentials: neither AZURE_FEDERATED_TOKEN_FILE (workload identity: label the pod and annotate the "
             "ServiceAccount) nor OAUTH_CLIENT_SECRET (client credentials: mount the Secret) is set")


MODE, credential = make_credential()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":  # readiness: one real token request per token lifetime, the rest is the cache
            try:
                credential.get()
                self._reply(200)
            except Exception as exc:
                log(error=f"token mint failed: {type(exc).__name__}: {exc}")
                self._reply(503)
            return
        self.do_any()

    def do_any(self):  # Envoy ext_authz: /check<original path>, with every client header forwarded
        if self.client_address[0] not in LOOPBACK:  # the port is reachable on the pod IP; never hand a token to another pod
            log(warning=f"refused token request from {self.client_address[0]}", path=self.path)
            self._reply(403)
            return
        try:  # attribution only: Envoy verified the signature, so just decode the payload segment
            seg = self.headers.get("x-langsmith-llm-auth", "").split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        except (IndexError, ValueError):
            claims = {}
        source = "failed"
        try:
            token, source = credential.get()
            status = 200
        except Exception as exc:  # deny; Envoy surfaces this to LangSmith as 403
            token, status = None, 503
            log(error=f"token mint failed: {type(exc).__name__}: {exc}")
        self._reply(status, token)
        line = {"ts": round(time.time(), 3), "status": status, "method": self.command,
                "path": self.path[len("/check"):] if self.path.startswith("/check") else self.path,
                "authority": self.headers.get("host"), "envoy_request_id": self.headers.get("x-request-id"),
                "token": source, **{k: claims.get(k) for k in LOGGED}}
        if LOG_HEADERS:
            line["headers"] = {k.lower(): (f"<redacted, {len(v)} chars>" if k.lower() in REDACT else v) for k, v in self.headers.items()}
            line["claims"] = claims
        log(**line)

    def _reply(self, status, token=None):
        self.send_response(status)
        if token:
            self.send_header("Authorization", f"Bearer {token}")  # injected into the upstream request
            self.send_header("x-envoy-auth-headers-to-remove", "x-langsmith-llm-auth, api-key")  # never reach Azure
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_any

    def log_message(self, *_):  # the JSON line replaces the default access log
        pass


if __name__ == "__main__":
    port = int(os.environ.get("LISTEN_PORT", "10002"))
    log(msg=f"llm-authz listening on :{port}", mode=MODE, client_id=credential.client_id, token_url=credential.url,
        scope=credential.scope or None, client_auth=credential.auth, log_headers=LOG_HEADERS)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
