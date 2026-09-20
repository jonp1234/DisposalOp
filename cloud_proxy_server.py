#!/usr/bin/env python3
"""
cloud_proxy_server.py — Cloud-deployable version of local_server_with_proxy.py,
for real field use (phones, anywhere, not just your own desktop).

WHAT'S DIFFERENT FROM THE LOCAL VERSION:
  - Reads its port from the $PORT environment variable (how Render, and
    most PaaS platforms, tell your app which port to listen on) instead of
    a fixed 8765.
  - API keys live ONLY as server-side environment variables (ORS_API_KEY,
    GOOGLE_API_KEY) -- injected into the outgoing request here, never sent
    to or visible in any browser. The app itself doesn't need a key typed
    into it at all anymore; this server supplies it.
  - Requires a shared secret (APP_SECRET env var) on every proxied request,
    checked against an X-App-Secret header the app sends. Without this,
    anyone who finds this service's public URL could hit it directly and
    burn through your API quota -- the local version was safe by accident
    (only reachable from your own machine); a real internet-facing one
    isn't, unless something like this checks who's actually asking.
  - Serves whatever HTML file is in this folder at "/" directly (rename
    your file to index.html when deploying) instead of a folder listing --
    for field use you want people visiting one clean URL, not browsing files.

-----------------------------------------------------------------------
DEPLOYING TO RENDER (or any similar PaaS)
-----------------------------------------------------------------------
1. Put this file, plus your HTML file renamed to index.html, in a folder
   (ideally a git repo -- Render deploys from a repo).
2. On Render: New -> Web Service -> connect your repo.
   - Runtime: Python 3
   - Start Command: python cloud_proxy_server.py
   - No build command needed -- this uses only the Python standard
     library, nothing to install.
3. Under Environment, add these (Render calls them "Environment Variables"):
     ORS_API_KEY     = your real ORS key
     GOOGLE_API_KEY   = your real Google key (only if you're using Google)
     APP_SECRET       = any long random string you make up -- this is
                         NOT an API key from anyone else, just a shared
                         password between this server and your app so
                         randos who find the URL can't use your quota.
4. Deploy. Render gives you a real URL like
   https://your-service-name.onrender.com

-----------------------------------------------------------------------
UPDATING YOUR HTML FILE TO MATCH
-----------------------------------------------------------------------
In CONFIG, point the base URLs at your real Render URL instead of
localhost, and set appSecret to the SAME value you put in Render's
APP_SECRET:
    orsBaseUrl: "https://your-service-name.onrender.com/proxy/ors",
    googleBaseUrl: "https://your-service-name.onrender.com/proxy/google",
    appSecret: "the same long random string from step 3",
Leave orsApiKey / googleApiKey blank -- this deployment doesn't need them
in the file at all anymore, since the server supplies them.
"""

import http.server
import socketserver
import urllib.request
import urllib.error
import os

PORT = int(os.environ.get("PORT", 8765))
APP_SECRET = os.environ.get("APP_SECRET", "")

# path prefix -> (real upstream host, env var holding the API key, header
# name that key goes in). Only these hosts can ever be reached through
# this proxy -- it does not forward to arbitrary URLs.
UPSTREAMS = {
    "/proxy/ors/": {
        "host": "https://api.heigit.org/openrouteservice/",
        "key_env": "ORS_API_KEY",
        "key_header": "Authorization",
    },
    "/proxy/google/": {
        "host": "https://routes.googleapis.com/",
        "key_env": "GOOGLE_API_KEY",
        "key_header": "X-Goog-Api-Key",
    },
}

# Headers that are about THIS hop (browser<->proxy) and shouldn't be blindly
# forwarded to the real upstream, or that we set ourselves on the reply.
HOP_BY_HOP = {"host", "content-length", "connection", "accept-encoding"}


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        if self._proxy_prefix():
            return self._proxy_request("GET")
        return super().do_GET()

    def do_POST(self):
        if self._proxy_prefix():
            return self._proxy_request("POST")
        self.send_response(404)
        self.end_headers()

    def _proxy_prefix(self):
        for prefix in UPSTREAMS:
            if self.path.startswith(prefix):
                return prefix
        return None

    def _proxy_request(self, method):
        # Reject anyone who doesn't know the shared secret, before doing
        # any real work or spending any quota on their behalf.
        if not APP_SECRET or self.headers.get("X-App-Secret") != APP_SECRET:
            body = b'{"error":{"message":"Missing or incorrect app secret"}}'
            self._relay_response(401, [("Content-Type", "application/json")], body)
            return

        prefix = self._proxy_prefix()
        config = UPSTREAMS[prefix]
        upstream_url = config["host"] + self.path[len(prefix):]

        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""

        fwd_headers = {}
        for key in self.headers:
            if key.lower() not in HOP_BY_HOP and key.lower() != "x-app-secret":
                fwd_headers[key] = self.headers[key]

        # Server-side key injection: whatever (if anything) the client sent
        # for auth is discarded and replaced with the real key from this
        # server's own environment variables -- the browser never sees it.
        real_key = os.environ.get(config["key_env"], "")
        if real_key:
            fwd_headers[config["key_header"]] = real_key

        req = urllib.request.Request(upstream_url, data=body, headers=fwd_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._relay_response(resp.status, resp.getheaders(), resp.read())
        except urllib.error.HTTPError as e:
            # A real error response FROM the upstream API -- relay it
            # through as-is so the app's own error handling still works.
            self._relay_response(e.code, e.headers.items() if e.headers else [], e.read())
        except Exception as e:
            err_body = (f'{{"error":{{"message":"Proxy could not reach {config["host"]}: '
                        f'{str(e)}"}}}}').encode()
            self._relay_response(502, [("Content-Type", "application/json")], err_body)

    def _relay_response(self, status, headers, body):
        self.send_response(status)
        for key, value in headers:
            if key.lower() not in HOP_BY_HOP and key.lower() != "access-control-allow-origin":
                self.send_header(key, value)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if self._proxy_prefix():
            print("[proxy]", fmt % args)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    if not APP_SECRET:
        print("WARNING: APP_SECRET is not set -- every proxied request will be rejected.")
        print("Set it as an environment variable before deploying for real use.")
    httpd = Server(("0.0.0.0", PORT), Handler)
    print(f"Serving on port {PORT}, with API proxying.")
    print("Proxy routes:")
    for prefix, config in UPSTREAMS.items():
        key_set = "set" if os.environ.get(config["key_env"]) else "NOT SET"
        print(f"  {prefix}  ->  {config['host']}  (key: {config['key_env']} is {key_set})")
    httpd.serve_forever()
