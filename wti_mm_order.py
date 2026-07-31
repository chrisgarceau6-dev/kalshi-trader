#!/usr/bin/env python3
"""WTI $100 crude — liquidity rewards market maker.

Places resting bid + ask within the 4.5c reward band and implements
"flat immediately" fill management: any fill triggers cancel of the
other side, accepting inventory rather than doubling down on a move.

CREDENTIALS (set as env vars):
    POLY_PRIVATE_KEY   your Polygon wallet private key (0x...)
    POLY_API_KEY       from app.polymarket.com → Settings → API
    POLY_API_SECRET    same source
    POLY_API_PASSPHRASE same source

    Get API keys: app.polymarket.com → Settings → API Keys → Create

TOKEN IDs (from wti_rewards_size.py output):
    YES token: 1051221457154018... (full ID required — see --show-token)

usage:
    python wti_mm_order.py --show-token        # print full token ID, no orders
    python wti_mm_order.py --dry-run           # print orders, no submission
    python wti_mm_order.py --size 100          # place 100-share bid+ask
    python wti_mm_order.py --size 100 --poll 30
"""
import argparse, hashlib, hmac, json, os, random, time
import requests
from eth_account import Account
from eth_account.messages import encode_defunct

CLOB      = "https://clob.polymarket.com"
GAMMA     = "https://gamma-api.polymarket.com"
CHAIN_ID  = 137
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
USDC_DEC  = 6   # USDC has 6 decimals on Polygon
NEG_RISK_CTF_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"


# ---------------------------------------------------------------------------
# auth helpers
# ---------------------------------------------------------------------------

def l2_headers(api_key, api_secret, passphrase, method, path, body=""):
    ts = str(int(time.time()))
    msg = ts + method.upper() + path + (body or "")
    sig = hmac.new(api_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return {
        "POLY_API_KEY":       api_key,
        "POLY_TIMESTAMP":     ts,
        "POLY_SIGNATURE":     sig,
        "POLY_PASSPHRASE":    passphrase,
        "Content-Type":       "application/json",
    }


# ---------------------------------------------------------------------------
# EIP-712 order signing
# ---------------------------------------------------------------------------

ORDER_TYPES = {
    "Order": [
        {"name": "salt",          "type": "uint256"},
        {"name": "maker",         "type": "address"},
        {"name": "signer",        "type": "address"},
        {"name": "taker",         "type": "address"},
        {"name": "tokenId",       "type": "uint256"},
        {"name": "makerAmount",   "type": "uint256"},
        {"name": "takerAmount",   "type": "uint256"},
        {"name": "expiration",    "type": "uint256"},
        {"name": "nonce",         "type": "uint256"},
        {"name": "feeRateBps",    "type": "uint256"},
        {"name": "side",          "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ]
}

DOMAIN = {
    "name":              "Polymarket CTF Exchange",
    "version":           "1",
    "chainId":           CHAIN_ID,
    "verifyingContract": CTF_EXCHANGE,
}


def sign_order(pk, maker, token_id, maker_amount, taker_amount, side_int):
    """
    side_int: 0 = BUY (bid), 1 = SELL (ask)
    BUY:  makerAmount=USDC_paid, takerAmount=YES_tokens_received
    SELL: makerAmount=YES_tokens_sold, takerAmount=USDC_received
    Amounts are in integer units (USDC 6-decimal, shares 6-decimal).
    """
    salt = random.randint(1, 2**128)
    order_data = {
        "salt":          salt,
        "maker":         maker,
        "signer":        maker,
        "taker":         "0x0000000000000000000000000000000000000000",
        "tokenId":       int(token_id),
        "makerAmount":   maker_amount,
        "takerAmount":   taker_amount,
        "expiration":    0,
        "nonce":         0,
        "feeRateBps":    0,
        "side":          side_int,
        "signatureType": 0,       # EOA signature
    }

    structured = {
        "types": {
            "EIP712Domain": [
                {"name": "name",              "type": "string"},
                {"name": "version",           "type": "string"},
                {"name": "chainId",           "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            **ORDER_TYPES,
        },
        "domain":      DOMAIN,
        "primaryType": "Order",
        "message":     order_data,
    }

    signed = Account.sign_typed_data(pk, full_message=structured)
    return order_data, signed.signature.hex()


def build_order_body(order_data, signature, owner, side_str):
    return json.dumps({
        "order": {
            "salt":          str(order_data["salt"]),
            "maker":         order_data["maker"],
            "signer":        order_data["signer"],
            "taker":         order_data["taker"],
            "tokenId":       str(order_data["tokenId"]),
            "makerAmount":   str(order_data["makerAmount"]),
            "takerAmount":   str(order_data["takerAmount"]),
            "expiration":    str(order_data["expiration"]),
            "nonce":         str(order_data["nonce"]),
            "feeRateBps":    str(order_data["feeRateBps"]),
            "side":          side_str,
            "signatureType": order_data["signatureType"],
            "signature":     "0x" + signature,
        },
        "owner":     owner,
        "orderType": "GTC",
    })


# ---------------------------------------------------------------------------
# CLOB REST calls
# ---------------------------------------------------------------------------

def place_order(body, headers):
    r = requests.post(f"{CLOB}/order", data=body, headers=headers, timeout=20)
    return r.status_code, r.json() if r.content else {}


def cancel_order(order_id, api_key, api_secret, passphrase):
    path = f"/order/{order_id}"
    hdrs = l2_headers(api_key, api_secret, passphrase, "DELETE", path)
    r = requests.delete(f"{CLOB}{path}", headers=hdrs, timeout=20)
    return r.status_code


def get_open_orders(maker, api_key, api_secret, passphrase):
    path = "/orders"
    hdrs = l2_headers(api_key, api_secret, passphrase, "GET", path)
    r = requests.get(f"{CLOB}{path}", params={"maker": maker}, headers=hdrs, timeout=20)
    return r.json() if r.status_code == 200 else []


def get_fills(maker, api_key, api_secret, passphrase, since_ts=0):
    path = "/fills"
    hdrs = l2_headers(api_key, api_secret, passphrase, "GET", path)
    r = requests.get(f"{CLOB}{path}", params={"maker": maker}, headers=hdrs, timeout=20)
    fills = r.json() if r.status_code == 200 else []
    return [f for f in fills if int(f.get("timestamp", 0)) > since_ts]


# ---------------------------------------------------------------------------
# find WTI token
# ---------------------------------------------------------------------------

def find_wti_token(pages=20):
    offset = 0
    for _ in range(pages):
        r = requests.get(f"{GAMMA}/markets", params={
            "closed": "false", "limit": 100, "offset": offset,
            "order": "volume24hr", "ascending": "false"}, timeout=30)
        if r.status_code != 200:
            break
        for m in r.json():
            q = str(m.get("question") or "").lower()
            if ("wti" in q or "crude oil" in q) and "100" in q and "july" in q:
                toks = m.get("clobTokenIds")
                if isinstance(toks, str):
                    import json as _j
                    toks = _j.loads(toks)
                if toks:
                    return toks[0], m
        offset += 100
        time.sleep(0.1)
    return None, None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--show-token", action="store_true")
    p.add_argument("--dry-run",    action="store_true")
    p.add_argument("--size",       type=int,   default=100)
    p.add_argument("--bid",        type=float, default=0.355)
    p.add_argument("--ask",        type=float, default=0.375)
    p.add_argument("--poll",       type=int,   default=30,
                   help="fill-check interval in seconds")
    a = p.parse_args()

    print("finding WTI $100 July token...")
    token_id, market = find_wti_token()
    if not token_id:
        print("ERROR: market not found. Run wti_rewards_size.py to verify it exists.")
        return
    print(f"token ID: {token_id}")
    print(f"market:   {market.get('question')}")

    if a.show_token:
        return

    # --- creds ---
    pk          = os.environ.get("POLY_PRIVATE_KEY", "")
    api_key     = os.environ.get("POLY_API_KEY", "")
    api_secret  = os.environ.get("POLY_API_SECRET", "")
    passphrase  = os.environ.get("POLY_API_PASSPHRASE", "")

    if not pk:
        print("\nERROR: POLY_PRIVATE_KEY not set.")
        print("  export POLY_PRIVATE_KEY=0x<your-key>")
        print("  export POLY_API_KEY=<key>")
        print("  export POLY_API_SECRET=<secret>")
        print("  export POLY_API_PASSPHRASE=<pass>")
        print("\nGet API keys: app.polymarket.com → Settings → API Keys")
        return

    acct  = Account.from_key(pk)
    maker = acct.address
    print(f"wallet:   {maker}")

    # BUY YES at bid: makerAmount=USDC, takerAmount=shares
    bid_maker = int(a.size * a.bid * 10**USDC_DEC)
    bid_taker = int(a.size             * 10**USDC_DEC)

    # SELL YES at ask: makerAmount=shares, takerAmount=USDC
    ask_maker = int(a.size             * 10**USDC_DEC)
    ask_taker = int(a.size * a.ask * 10**USDC_DEC)

    print(f"\norders to place:")
    print(f"  BID {a.size} shares @ {a.bid}  "
          f"(USDC locked: ${a.size * a.bid:.2f})")
    print(f"  ASK {a.size} shares @ {a.ask}  "
          f"(NO collateral locked: ${a.size * (1 - a.ask):.2f})")
    print(f"  total capital: ${a.size * a.bid + a.size * (1 - a.ask):.2f}")

    if a.dry_run:
        bid_data, bid_sig = sign_order(pk, maker, token_id, bid_maker, bid_taker, 0)
        ask_data, ask_sig = sign_order(pk, maker, token_id, ask_maker, ask_taker, 1)
        print("\n[dry-run] bid order struct (no submission):")
        print(json.dumps(bid_data, indent=2))
        print("\n[dry-run] ask order struct (no submission):")
        print(json.dumps(ask_data, indent=2))
        return

    # --- place bid ---
    bid_data, bid_sig = sign_order(pk, maker, token_id, bid_maker, bid_taker, 0)
    bid_body = build_order_body(bid_data, bid_sig, maker, "BUY")
    bid_hdrs = l2_headers(api_key, api_secret, passphrase, "POST", "/order", bid_body)

    print("\nplacing BID...")
    code, resp = place_order(bid_body, bid_hdrs)
    print(f"  HTTP {code}: {json.dumps(resp)[:200]}")
    bid_order_id = resp.get("orderID") or resp.get("id") or resp.get("orderId")

    # --- place ask ---
    ask_data, ask_sig = sign_order(pk, maker, token_id, ask_maker, ask_taker, 1)
    ask_body = build_order_body(ask_data, ask_sig, maker, "SELL")
    ask_hdrs = l2_headers(api_key, api_secret, passphrase, "POST", "/order", ask_body)

    print("placing ASK...")
    code, resp = place_order(ask_body, ask_hdrs)
    print(f"  HTTP {code}: {json.dumps(resp)[:200]}")
    ask_order_id = resp.get("orderID") or resp.get("id") or resp.get("orderId")

    if not bid_order_id or not ask_order_id:
        print("\nERROR: one or both orders failed to place. Check response above.")
        print("Common issues: insufficient USDC balance, API creds wrong, "
              "market closed.")
        return

    print(f"\norders live:")
    print(f"  bid ID: {bid_order_id}")
    print(f"  ask ID: {ask_order_id}")
    print(f"\npolling for fills every {a.poll}s  (Ctrl-C to stop and cancel both)")

    start_ts  = int(time.time())
    bid_alive = True
    ask_alive = True

    try:
        while bid_alive or ask_alive:
            time.sleep(a.poll)
            fills = get_fills(maker, api_key, api_secret, passphrase, start_ts)
            filled_ids = {f.get("orderId") for f in fills}

            bid_filled = bid_order_id in filled_ids
            ask_filled = ask_order_id in filled_ids

            if bid_filled and bid_alive:
                print(f"\n[{time.strftime('%H:%M:%S')}] BID FILLED — cancelling ask")
                cancel_order(ask_order_id, api_key, api_secret, passphrase)
                ask_alive = False
                bid_alive = False
                inv = a.size
                print(f"  inventory: LONG {inv} YES shares @ avg {a.bid:.3f}")
                print(f"  at current mid ~{(a.bid+a.ask)/2:.3f} that's "
                      f"${inv * ((a.bid+a.ask)/2 - a.bid):.2f} mark-to-market")
                print("  decision: hold, sell on the book, or cancel now?")
                break

            if ask_filled and ask_alive:
                print(f"\n[{time.strftime('%H:%M:%S')}] ASK FILLED — cancelling bid")
                cancel_order(bid_order_id, api_key, api_secret, passphrase)
                bid_alive = False
                ask_alive = False
                inv = a.size
                print(f"  inventory: SHORT {inv} YES shares (long NO) @ avg {a.ask:.3f}")
                print(f"  at current mid ~{(a.bid+a.ask)/2:.3f} that's "
                      f"${inv * (a.ask - (a.bid+a.ask)/2):.2f} mark-to-market")
                break

            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] no fills  bid={'live' if bid_alive else 'done'}  "
                  f"ask={'live' if ask_alive else 'done'}", end="\r")

    except KeyboardInterrupt:
        print("\n\ninterrupted — cancelling both orders...")
        if bid_alive:
            c = cancel_order(bid_order_id, api_key, api_secret, passphrase)
            print(f"  cancel bid: HTTP {c}")
        if ask_alive:
            c = cancel_order(ask_order_id, api_key, api_secret, passphrase)
            print(f"  cancel ask: HTTP {c}")
        print("done.")


if __name__ == "__main__":
    main()
