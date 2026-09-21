#!/usr/bin/env python3
"""
cloud_proxy_server_multitenant.py — Same idea as cloud_proxy_server.py
(a cloud-deployed proxy so ORS/Google work from any phone, anywhere), but
supports MULTIPLE CUSTOMERS from one single deployment, each with their
own API key(s) — instead of one shared key for everyone.

HOW IT WORKS: instead of one fixed APP_SECRET checked against one fixed
key, this holds a LOOKUP TABLE of secret -> that customer's own keys.
Each customer gets a distinct secret baked into their own copy of the
HTML file (everything else about their file is identical — same URL,
same code); the server looks up which customer is asking based on which
secret came in, and injects THEIR key, never anyone else's.

Adding a new customer later is just adding one entry to the lookup table
below (as an environment variable) and giving them their own copy of the
HTML file with their own secret — no new deployment, no new URL, nothing
else changes.

-----------------------------------------------------------------------
CONFIGURING CUSTOMERS
-----------------------------------------------------------------------
One environment variable, CUSTOMERS_JSON, holds all of them as a JSON
object. Each key is a customer's secret (make these up — long, random,
unique per customer, never reused); each value has that customer's own
keys:

    {
      "customer-a-long-random-secret": {
        "name": "Customer A",
        "ors_key": "customer A's real ORS key",
        "google_key": "customer A's real Google key (omit if they don't use Google)"
      },
      "customer-b-long-random-secret": {
        "name": "Customer B",
        "ors_key": "customer B's real ORS key"
      }
    }

On Render: Environment -> add CUSTOMERS_JSON with that whole JSON blob as
the value. Editing it and saving restarts the service with the new list
picked up -- no code redeploy needed to add/remove a customer.

-----------------------------------------------------------------------
SETTING UP EACH CUSTOMER'S COPY OF THE HTML FILE
-----------------------------------------------------------------------
Same file for everyone, same orsBaseUrl/googleBaseUrl (your one Render
URL) -- the ONLY thing that differs per customer is CONFIG.appSecret:
    appSecret: "customer-a-long-random-secret",
Give Customer A that copy, Customer B a copy with their own secret, etc.
Leave orsApiKey/googleApiKey blank in every copy -- the server supplies
the right one based on which secret shows up.
"""

import http.server
import socketserver
import urllib.request
import urllib.error
import os
import json

PORT = int(os.environ.get("PORT", 8765))

try:
    CUSTOMERS = json.loads(os.environ.get("CUSTOMERS_JSON", "{}"))
except json.JSONDecodeError as e:
    print(f"WARNING: CUSTOMERS_JSON is not valid JSON ({e}) -- treating as empty, ALL requests will be rejected.")
    CUSTOMERS = {}

# path prefix -> (real upstream host, which key field in a customer's
# entry to use, header name that key goes in)
UPSTREAMS = {
    "/proxy/ors/": {
        "host": "https://api.heigit.org/openrouteservice/",
        "key_field": "ors_key",
        "key_header": "Authorization",
    },
    "/proxy/google/": {
        "host": "https://routes.googleapis.com/",
        "key_field": "google_key",
        "key_header": "X-Goog-Api-Key",
    },
}

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
        secret = self.headers.get("X-App-Secret", "")
        customer = CUSTOMERS.get(secret)
        if not customer:
            body = b'{"error":{"message":"Unrecognized or missing app secret"}}'
            self._relay_response(401, [("Content-Type", "application/json")], body)
            return

        prefix = self._proxy_prefix()
        config = UPSTREAMS[prefix]
        real_key = customer.get(config["key_field"])
        if not real_key:
            # This customer exists, but hasn't been given a key for THIS
            # particular provider (e.g. they use ORS but not Google).
            body = (f'{{"error":{{"message":"No {config["key_field"]} configured for this customer"}}}}').encode()
            self._relay_response(403, [("Content-Type", "application/json")], body)
            return

        upstream_url = config["host"] + self.path[len(prefix):]

        body = None
        if method == "POST":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""

        fwd_headers = {}
        for key in self.headers:
            if key.lower() not in HOP_BY_HOP and key.lower() != "x-app-secret":
                fwd_headers[key] = self.headers[key]
        fwd_headers[config["key_header"]] = real_key

        req = urllib.request.Request(upstream_url, data=body, headers=fwd_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._relay_response(resp.status, resp.getheaders(), resp.read())
            print(f"[proxy] {customer.get('name', '(unnamed)')} -> {prefix} OK")
        except urllib.error.HTTPError as e:
            self._relay_response(e.code, e.headers.items() if e.headers else [], e.read())
            print(f"[proxy] {customer.get('name', '(unnamed)')} -> {prefix} upstream error {e.code}")
        except Exception as e:
            err_body = (f'{{"error":{{"message":"Proxy could not reach {config["host"]}: '
                        f'{str(e)}"}}}}').encode()
            self._relay_response(502, [("Content-Type", "application/json")], err_body)
            print(f"[proxy] {customer.get('name', '(unnamed)')} -> {prefix} proxy error: {e}")

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
        pass  # per-request logging is handled explicitly above, with customer names


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    if not CUSTOMERS:
        print("WARNING: CUSTOMERS_JSON is empty or unset -- every proxied request will be rejected.")
    httpd = Server(("0.0.0.0", PORT), Handler)
    print(f"Serving on port {PORT}, with API proxying for {len(CUSTOMERS)} customer(s).")
    for secret, info in CUSTOMERS.items():
        keys_present = [f["key_field"] for f in UPSTREAMS.values() if info.get(f["key_field"])]
        print(f"  {info.get('name', '(unnamed)')}: has keys for {keys_present or 'NONE'}")
    httpd.serve_forever()
