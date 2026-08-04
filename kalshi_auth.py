#!/usr/bin/env python3
"""Kalshi API auth helper — signs requests using RSA-PSS-SHA256.

SETUP (one-time, ~3 min):
    1. Generate RSA keypair (I'll show command below)
    2. Upload public key to app.kalshi.com → Account → API Keys
    3. Copy the "Access Key ID" they give you
    4. Set env vars:
         export KALSHI_API_KEY_ID=<the-uuid-they-give-you>
         export KALSHI_PRIVATE_KEY_PATH=~/.kalshi/private_key.pem

usage as CLI:
    python kalshi_auth.py setup       # print keygen commands
    python kalshi_auth.py test        # test signed GET /portfolio/balance
    python kalshi_auth.py place --ticker <t> --side yes --count 100 --yes-price 55
"""
import argparse, base64, os, sys, time, json
from pathlib import Path
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


def load_private_key(path=None):
    path = path or os.environ.get("KALSHI_PRIVATE_KEY_PATH", "~/.kalshi/private_key.pem")
    path = Path(os.path.expanduser(path))
    if not path.exists():
        raise FileNotFoundError(f"Kalshi private key not found at {path}. Run: python kalshi_auth.py setup")
    with open(path, 'rb') as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_request(private_key, method, path, timestamp_ms=None):
    """Sign a request using RSA-PSS-SHA256."""
    ts = str(timestamp_ms or int(time.time() * 1000))
    msg = ts + method.upper() + "/trade-api/v2" + path
    sig = private_key.sign(
        msg.encode('utf-8'),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return ts, base64.b64encode(sig).decode()


def signed_headers(method, path, api_key_id=None, private_key=None):
    api_key_id = api_key_id or os.environ.get("KALSHI_API_KEY_ID", "")
    if not api_key_id:
        raise ValueError("KALSHI_API_KEY_ID not set")
    private_key = private_key or load_private_key()
    ts, sig = sign_request(private_key, method, path)
    return {
        "KALSHI-ACCESS-KEY":       api_key_id,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Accept":                  "application/json",
        "Content-Type":            "application/json",
    }


def _parse(r):
    try:
        return r.json() if r.content else {}
    except Exception:
        return {"_raw": r.text[:200]}


def get(path, params=None):
    hdrs = signed_headers("GET", path)
    r = requests.get(KALSHI + path, headers=hdrs, params=params, timeout=20)
    return r.status_code, _parse(r)


def post(path, body):
    hdrs = signed_headers("POST", path)
    r = requests.post(KALSHI + path, headers=hdrs, json=body, timeout=20)
    return r.status_code, _parse(r)


def delete(path):
    hdrs = signed_headers("DELETE", path)
    r = requests.delete(KALSHI + path, headers=hdrs, timeout=20)
    return r.status_code, _parse(r)


def cancel_order(order_id):
    """Cancel an open GTC order by order_id. Returns (status_code, response)."""
    return delete(f"/portfolio/orders/{order_id}")


# ---------------------------------------------------------------- orders

def get_balance():
    return get("/portfolio/balance")


def place_order(ticker, side, count, yes_price_cents=None, no_price_cents=None,
                order_type="limit", action="buy", time_in_force="GTC"):
    """Place an order on Kalshi using the V2 endpoint.
    - ticker: market ticker, e.g. 'KXETH15M-26JUL271100-T3491.25'
    - side: 'yes' or 'no' (which token to buy)
    - count: number of contracts
    - yes_price_cents: 1-99 limit price in cents when side=yes
    - no_price_cents: 1-99 limit price in cents when side=no
    """
    # V2 always quotes from the YES side: bid=buy YES, ask=sell YES (= buy NO)
    v2_side = "bid" if side == "yes" else "ask"

    if side == "yes" and yes_price_cents is not None:
        price_str = f"{int(yes_price_cents) / 100:.4f}"
    elif side == "no" and no_price_cents is not None:
        # buying NO at X¢ = selling YES at (100−X)¢
        price_str = f"{(100 - int(no_price_cents)) / 100:.4f}"
    else:
        price_str = "0.5000"

    body = {
        "ticker":                    ticker,
        "side":                      v2_side,
        "count":                     str(int(count)),
        "price":                     price_str,
        "time_in_force":             "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id":           f"arb-{int(time.time()*1000)}",
    }
    return post("/portfolio/events/orders", body)


# ---------------------------------------------------------------- CLI

def cmd_setup(a):
    print("KALSHI API SETUP — one-time (~3 min)\n")
    print("1. Generate an RSA keypair:")
    print("     mkdir -p ~/.kalshi")
    print("     openssl genrsa -out ~/.kalshi/private_key.pem 2048")
    print("     openssl rsa -in ~/.kalshi/private_key.pem -pubout -out ~/.kalshi/public_key.pem")
    print("     chmod 600 ~/.kalshi/private_key.pem")
    print("\n2. Copy the public key contents:")
    print("     cat ~/.kalshi/public_key.pem")
    print("\n3. Go to app.kalshi.com → Account → API Keys → Create New")
    print("   Paste the public key. Kalshi gives you an 'Access Key ID' (a UUID).")
    print("\n4. Set env vars (add to ~/.zshrc for persistence):")
    print("     export KALSHI_API_KEY_ID=<uuid-from-kalshi>")
    print("     export KALSHI_PRIVATE_KEY_PATH=~/.kalshi/private_key.pem")
    print("\n5. Test it:")
    print("     python kalshi_auth.py test")


def cmd_test(a):
    try:
        code, j = get_balance()
        print(f"HTTP {code}")
        print(json.dumps(j, indent=2))
        if code == 200 and 'balance' in str(j).lower():
            print("\n✓ Auth working. You're ready to place orders.")
    except Exception as e:
        print(f"ERROR: {e}")
        print("Run: python kalshi_auth.py setup")


def cmd_place(a):
    price = a.yes_price if a.side == "yes" else a.no_price
    print(f"Placing: {a.ticker} buy {a.side} x{a.count} @ {price}¢")
    if a.dry_run:
        print("[dry-run] not submitting")
        return
    code, j = place_order(a.ticker, a.side, a.count,
                          yes_price_cents=a.yes_price, no_price_cents=a.no_price)
    print(f"HTTP {code}")
    print(json.dumps(j, indent=2))


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup").set_defaults(fn=cmd_setup)
    sub.add_parser("test").set_defaults(fn=cmd_test)
    s = sub.add_parser("place")
    s.add_argument("--ticker", required=True)
    s.add_argument("--side", choices=['yes','no'], required=True)
    s.add_argument("--count", type=int, required=True)
    s.add_argument("--yes-price", type=int, help="limit price for YES in cents (1-99)")
    s.add_argument("--no-price", type=int, help="limit price for NO in cents (1-99)")
    s.add_argument("--action", choices=['buy','sell'], default='buy')
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_place)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
