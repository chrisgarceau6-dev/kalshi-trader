#!/usr/bin/env python3
"""Derive Polymarket L2 API credentials from your private key.
Bypasses the web UI entirely.

usage:
    export POLY_PRIVATE_KEY=0x<your-key>
    python poly_get_apikey.py
"""
import os, time
import requests
from eth_account import Account
from eth_account.messages import encode_defunct

CLOB = "https://clob.polymarket.com"


def derive_api_key(pk, nonce=0):
    acct = Account.from_key(pk)
    ts   = str(int(time.time()))
    path = "/auth/api-key"
    msg  = encode_defunct(text=f"{ts}GET{path}")
    sig  = Account.sign_message(msg, private_key=pk).signature.hex()

    r = requests.get(f"{CLOB}{path}", params={"nonce": nonce}, headers={
        "POLY_ADDRESS":   acct.address,
        "POLY_SIGNATURE": "0x" + sig,
        "POLY_TIMESTAMP": ts,
        "POLY_NONCE":     str(nonce),
    }, timeout=20)

    print(f"HTTP {r.status_code}")
    if r.status_code == 200:
        j = r.json()
        print(f"\nexport POLY_API_KEY={j.get('apiKey', j.get('key', ''))}")
        print(f"export POLY_API_SECRET={j.get('secret', '')}")
        print(f"export POLY_API_PASSPHRASE={j.get('passphrase', '')}")
        print(f"export POLY_PRIVATE_KEY={pk}")
        return j
    else:
        print(r.text[:400])
        return None


if __name__ == "__main__":
    pk = os.environ.get("POLY_PRIVATE_KEY", "")
    if not pk:
        print("set POLY_PRIVATE_KEY first")
    else:
        derive_api_key(pk)
