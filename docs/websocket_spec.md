# Kalshi WebSocket market-state client contract

Status: implementation specification  
Target: Kalshi event-contract Trade API, one process, one active trader, approximately six live markets  
Safety rule: **only `LIVE` permits new entries; every other state fails closed**

The words **MUST**, **MUST NOT**, **SHOULD**, and **MAY** are normative. Values labeled **client policy** are deliberately chosen application limits, not limits promised by Kalshi.

## 1. Connection and authentication

### 1.1 Endpoints

- Production: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`
- Demo: `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`

Kalshi also documents older shared hosts as supported, but this client MUST use the dedicated hosts above. Source: [Quick Start: WebSockets](https://docs.kalshi.com/getting_started/quick_start_websockets).

### 1.2 Upgrade-handshake authentication

Authentication is carried in HTTP headers on the WebSocket upgrade request. It is not a query parameter and not a JSON authentication message.

Required headers:

```text
KALSHI-ACCESS-KEY: <API key ID>
KALSHI-ACCESS-SIGNATURE: <Base64 RSA-PSS signature>
KALSHI-ACCESS-TIMESTAMP: <Unix epoch milliseconds as a decimal string>
```

For the WebSocket handshake, construct the exact UTF-8 signature message as:

```text
<timestamp>GET/trade-api/ws/v2
```

There are no separators. Sign it with RSA-PSS using SHA-256, MGF1-SHA256, and a salt length equal to the SHA-256 digest length; Base64-encode the resulting bytes. Sources: [WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets) and [API keys/signing](https://docs.kalshi.com/getting_started/api_keys).

The timestamp used in the signature MUST be the exact string placed in `KALSHI-ACCESS-TIMESTAMP`. Generate new headers for every connection attempt. Never reuse a signature during reconnect.

### 1.3 Keepalive

Kalshi sends WebSocket Ping control frames, opcode `0x9`, every 10 seconds with payload `heartbeat`. The client MUST reply with a Pong control frame, opcode `0xA`. Kalshi's page does not specify the Pong payload; the client MUST follow the underlying WebSocket protocol and echo the Ping's application data in the Pong. The client MAY send Ping frames; Kalshi documents that it responds with Pong. Sources: [Kalshi Connection Keep-Alive](https://docs.kalshi.com/websockets/connection-keep-alive) and [RFC 6455 section 5.5.3](https://www.rfc-editor.org/rfc/rfc6455#section-5.5.3).

The server's Pong deadline, close code, and close reason after a missing Pong are **not documented — must be determined empirically**. The client MUST NOT depend on the server to detect a dead connection.

Client policy:

- Record `last_ping_rx_monotonic` whenever a server Ping is received.
- If no server Ping has been received for `15_000 ms`, enter `STALE`, block entries, close the socket, and reconnect.
- Use the WebSocket library's automatic Pong only if tests prove that it echoes the payload. Otherwise handle Ping/Pong explicitly.
- Do not use absence of market-data messages as a liveness failure; an unchanged market can legitimately be quiet.

### 1.4 Reconnect

Kalshi recommends automatic reconnection with exponential backoff but does not document a schedule. Source: [WebSocket quick start](https://docs.kalshi.com/getting_started/quick_start_websockets).

Client policy:

```text
base delays: 0, 0.5, 1, 2, 4, 8, 16, 30, 30, ... seconds
actual delay: uniform random value in [0, base delay] (full jitter)
reset attempts: only after the connection has remained LIVE for 60 seconds
connect timeout: 10 seconds
subscription acknowledgement timeout: 5 seconds
initial orderbook snapshot timeout: 5 seconds after its subscribed response
```

Every disconnect, authentication failure, Ping timeout, malformed safety-critical message, buffer overflow, or sequence gap MUST invalidate every local book. Reconnection always creates a new session, new command IDs, new subscription IDs, and new snapshots. No state from the old session may become tradable again.

### 1.5 Documented limits

The official changelog documents:

- WebSocket connections per user are tier-dependent; the default starts at 200.
- Orderbook subscriptions are limited to 500,000 market subscriptions per session.
- WebSocket commands are limited to 10,000 per second.

Source: [Kalshi API changelog](https://docs.kalshi.com/changelog) entries dated September 24, 2025 and June 18, 2026. The tier-by-tier WebSocket connection limits and the exact meaning of one "market subscription" are **not documented**. Error code `26` means the per-subscription market limit would be exceeded; code `27` means too many subscription commands. Source: [WebSocket connection/error schema](https://docs.kalshi.com/websockets/websocket-connection).

This client uses one connection, four channel subscriptions, and approximately six markets, so it MUST NOT implement connection pooling.

## 2. Subscription protocol

All commands are JSON text messages. Send one channel per subscribe command so each command ID maps unambiguously to one returned subscription ID.

### 2.1 IDs

- `id` is a client-generated integer command correlation ID, unique within the WebSocket session. Start at `1` and increment for every command. Kalshi treats `id: 0` as no ID, so this client MUST NOT use zero.
- `sid` is a server-generated subscription ID identifying a channel subscription.
- `seq`, where present, is a server-generated sequence number. Its documented and undocumented semantics are specified in section 4.

Source: [WebSocket connection schema](https://docs.kalshi.com/websockets/websocket-connection).

### 2.2 Required subscriptions

Substitute the current market tickers in `market_tickers`.

Orderbook, with the pricing convention pinned explicitly:

```json
{
  "id": 1,
  "cmd": "subscribe",
  "params": {
    "channels": ["orderbook_delta"],
    "market_tickers": ["MARKET-1", "MARKET-2"],
    "use_yes_price": true
  }
}
```

Ticker, including an initial value for each requested market:

```json
{
  "id": 2,
  "cmd": "subscribe",
  "params": {
    "channels": ["ticker"],
    "market_tickers": ["MARKET-1", "MARKET-2"],
    "send_initial_snapshot": true
  }
}
```

All fills for the authenticated account/subaccount scope:

```json
{
  "id": 3,
  "cmd": "subscribe",
  "params": {
    "channels": ["fill"]
  }
}
```

All user-order updates:

```json
{
  "id": 4,
  "cmd": "subscribe",
  "params": {
    "channels": ["user_orders"]
  }
}
```

`orderbook_delta` requires a ticker filter and accepts `market_ticker` for one or `market_tickers` for many. `ticker`, `fill`, and `user_orders` permit filters; omitting the filter from the two private channels avoids losing updates when the active market set changes. Sources: [Orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates), [Market ticker](https://docs.kalshi.com/websockets/market-ticker), [User fills](https://docs.kalshi.com/websockets/user-fills), and [User orders](https://docs.kalshi.com/websockets/user-orders).

One connection can carry multiple channels and multiple markets. Kalshi describes all WebSocket communication as occurring through a single connection and supports `market_tickers` arrays. The maximum number of distinct channel subscriptions per connection is **not documented**. Source: [WebSocket connection](https://docs.kalshi.com/websockets/websocket-connection).

### 2.3 Subscribe response

For each channel, expect:

```json
{
  "id": 1,
  "type": "subscribed",
  "msg": {
    "channel": "orderbook_delta",
    "sid": 1
  }
}
```

Correlate it to the pending command by `id`; retain the returned `sid`. The relative ordering of the `subscribed` response and the first channel data message is **not documented — must be determined empirically**. The parser MUST tolerate either order while remaining in `SYNCING`.

`orderbook_delta` explicitly sends an `orderbook_snapshot` before incremental deltas for each market. `ticker` sends an initial ticker value only when `send_initial_snapshot: true`. The docs do not describe initial snapshots for `fill` or `user_orders`; treat both as forward-only notification streams, not historical state. Sources: [Orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates), [Market ticker](https://docs.kalshi.com/websockets/market-ticker), [User fills](https://docs.kalshi.com/websockets/user-fills), and [User orders](https://docs.kalshi.com/websockets/user-orders).

### 2.4 Unsubscribe and inspect

Unsubscribe by server-generated subscription IDs:

```json
{
  "id": 5,
  "cmd": "unsubscribe",
  "params": {
    "sids": [1, 2]
  }
}
```

Success is:

```json
{
  "id": 5,
  "sid": 1,
  "seq": 7,
  "type": "unsubscribed"
}
```

List active subscriptions:

```json
{"id": 6, "cmd": "list_subscriptions"}
```

The response is `{"id":6,"type":"ok","msg":[{"channel":"...","sid":1}]}`. Source: [WebSocket connection](https://docs.kalshi.com/websockets/websocket-connection).

This implementation SHOULD reconnect rather than dynamically replace the six-market universe. If dynamic updates are later needed, `update_subscription` accepts exactly one `sid` (or a one-element `sids` array), `market_tickers`, and `action: "add_markets" | "delete_markets" | "get_snapshot"`. For orderbook only, `get_snapshot` returns snapshots without changing the subscription. The docs do not define whether `get_snapshot` alone provides a race-free recovery after a missed delta; therefore this client MUST use a full reconnect for gap recovery.

## 3. Message contracts

### 3.1 Numeric representation

All price fields ending in `_dollars` are decimal strings with up to four decimal places. All contract quantities ending in `_fp` are decimal strings with up to two decimal places. Integer-cent price fields cannot represent sub-cent ticks. Source: [Fixed-Point Representation](https://docs.kalshi.com/getting_started/fixed_point_migration).

The implementation MUST NOT parse any price or quantity through an IEEE-754 float.

Use these exact internal units:

```text
price_u = exact decimal dollars * 10,000      # integer; $1.0000 == 10,000
qty_u   = exact decimal contracts * 100       # integer; 1.00 contract == 100
ONE     = 10,000
```

Reject a value with more precision than its field permits, a negative snapshot quantity, or a price outside `[0, ONE]`. Market-valid tick sizes are supplied by each market's REST `price_ranges`; the client MUST load them and MUST NOT assume whole-cent ticks. Source: [Fixed-Point Representation](https://docs.kalshi.com/getting_started/fixed_point_migration).

### 3.2 `orderbook_delta`

Snapshot envelope:

```ts
{
  type: "orderbook_snapshot";
  sid: number;                 // integer >= 1
  seq: number;                 // integer >= 1
  msg: {
    market_ticker: string;
    market_id: string;         // UUID
    yes_dollars_fp?: [string, string][]; // [price_dollars, count_fp]
    no_dollars_fp?:  [string, string][]; // [price_dollars, count_fp]
  };
}
```

The two arrays are optional and may be absent when that side is empty. Delta envelope:

```ts
{
  type: "orderbook_delta";
  sid: number;
  seq: number;
  msg: {
    market_ticker: string;
    market_id: string;         // UUID
    price_dollars: string;
    delta_fp: string;          // signed fixed-point contract increment, 2 dp
    side: "yes" | "no";
    client_order_id?: string;  // only when this user's order caused the change
    subaccount?: number;       // may accompany own-order change
    ts?: string;               // deprecated RFC3339
    ts_ms?: number;            // Unix epoch ms; optional
  };
}
```

`ts_ms` is described as the time the orderbook change was recorded and is exchange-side. No receive timestamp is supplied. The client MUST add `recv_wall_ms` and `recv_monotonic_ns` immediately when its socket callback receives every frame. Source: [Orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates).

### 3.3 `ticker`

```ts
{
  type: "ticker";
  sid: number;
  msg: {
    market_ticker: string;
    market_id: string;          // UUID
    price_dollars: string;      // last trade
    yes_bid_dollars: string;
    yes_ask_dollars: string;
    volume_fp: string;
    open_interest_fp: string;
    dollar_volume: number;      // integer
    dollar_open_interest: number; // integer
    yes_bid_size_fp: string;
    yes_ask_size_fp: string;
    last_trade_size_fp: string;
    ts: number;                 // deprecated Unix seconds
    ts_ms: number;              // Unix epoch ms
    time: string;               // deprecated RFC3339
  };
}
```

Ticker messages do **not** contain `seq` in the documented schema. They are sent whenever any ticker field changes. `ts_ms` is exchange-side; add local receive timestamps. Ticker is a cross-check/top-of-book convenience feed, not the reconstructed depth source. Source: [Market ticker](https://docs.kalshi.com/websockets/market-ticker).

### 3.4 `fill`

The published AsyncAPI schema is:

```ts
{
  type: "fill";
  sid: number;
  msg: {
    trade_id: string;           // UUID; fill deduplication key
    order_id: string;           // UUID
    market_ticker: string;
    exchange_index: number;
    is_taker: boolean;
    side: "yes" | "no";        // legacy
    yes_price_dollars: string;
    count_fp: string;
    fee_cost: string;           // fixed-point dollars
    action: "buy" | "sell";    // legacy
    ts: number;                 // deprecated Unix seconds
    ts_ms: number;              // Unix epoch ms
    client_order_id?: string;
    post_position_fp: string;
    purchased_side: "yes" | "no"; // legacy
    outcome_side: "yes" | "no";
    book_side: "bid" | "ask";
    subaccount?: number;
  };
}
```

The schema marks `fee_cost`, `outcome_side`, and `book_side` required, but the example on the same page omits them. This is a documentation inconsistency. Until the demo probe confirms production behavior, the decoder MUST accept those three as absent, preserve unknown fields, and emit a schema-drift metric. It MUST use `trade_id` for idempotent fill ingestion. A missing/invalid `trade_id`, `order_id`, `market_ticker`, `yes_price_dollars`, `count_fp`, or `ts_ms` makes that notification unusable and MUST trigger immediate REST fill/order reconciliation; it must not independently mutate authoritative position state. Source: [User fills](https://docs.kalshi.com/websockets/user-fills).

Fill messages do **not** contain `seq` in the documented schema. Therefore absence of a WebSocket fill message never proves absence of a fill.

### 3.5 `user_orders`

The published AsyncAPI schema is:

```ts
{
  type: "user_order";          // channel name is "user_orders"
  sid: number;
  msg: {
    order_id: string;           // UUID
    user_id: string;            // UUID
    ticker: string;             // note: not market_ticker
    status: "resting" | "canceled" | "executed";
    side: "yes" | "no";        // legacy
    is_yes: boolean;            // deprecated
    outcome_side: "yes" | "no";
    book_side: "bid" | "ask";
    yes_price_dollars: string;  // 4 dp fixed-point dollars
    fill_count_fp: string;
    remaining_count_fp: string;
    initial_count_fp: string;
    taker_fill_cost_dollars: string;
    maker_fill_cost_dollars: string;
    taker_fees_dollars: string;
    maker_fees_dollars: string;
    client_order_id: string;
    order_group_id?: string;
    self_trade_prevention_type?: "taker_at_cross" | "maker";
    created_time: string;       // deprecated RFC3339
    created_ts_ms: number;
    last_update_time?: string;  // deprecated RFC3339
    last_updated_ts_ms?: number;
    expiration_time?: string;   // deprecated RFC3339
    expiration_ts_ms?: number;
    subaccount_number?: number;
  };
}
```

The example on the same page omits several fields that its schema marks required, including `outcome_side`, `book_side`, `taker_fees_dollars`, and `maker_fees_dollars`. The decoder MUST therefore tolerate absent non-identity fields and emit a schema-drift metric. `order_id`, `ticker`, `status`, and `client_order_id` are the minimum useful identity/lifecycle fields; if any is absent, trigger REST reconciliation and do not finalize local order state from the message. Source: [User orders](https://docs.kalshi.com/websockets/user-orders).

User-order messages do **not** contain `seq` in the documented schema. `created_ts_ms`, `last_updated_ts_ms`, and `expiration_ts_ms` describe the order lifecycle at the exchange; there is no documented envelope receive/event timestamp. Add local receive timestamps.

### 3.6 Unknown and error messages

The decoder MUST ignore-but-log unknown additive fields. It MUST NOT crash on an unknown message `type`; it MUST record the complete raw message with secrets redacted.

For `{"type":"error","msg":{"code":N,"msg":"..."}}`:

- Codes `10`, `17`, `18`, `25`, or any unknown code: leave `LIVE`, invalidate books, and reconnect.
- Code `25` specifically means subscription buffer overflow; local state may have missed events.
- Code `26`: configuration failure because the market limit was exceeded; fail closed and alert.
- Code `27`: command-rate failure; fail closed and reconnect using normal backoff.
- User/configuration errors `1`–`9`, `11`–`16`, and `19`–`24`: fail closed and alert; do not retry indefinitely without changing the request.

Source: [WebSocket error codes](https://docs.kalshi.com/websockets/websocket-connection).

## 4. Sequence semantics and gap policy

Only `orderbook_snapshot` and `orderbook_delta` include `seq` among the four requested data channels. The docs say it is a sequential number to check for complete delivery and snapshot/delta consistency. They do **not** state whether it is monotonic per connection, per `sid`, or per market — must be determined empirically. Sources: [Orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates) and the four channel schemas linked above.

Until the probe establishes otherwise, implement the conservative rule that matches the protocol envelope:

1. Track `last_seq` independently for each orderbook `sid`, across all markets on that `sid`.
2. The first accepted message for each market MUST be `orderbook_snapshot`. Accept its `seq` as part of the same `sid` stream.
3. After the first sequenced message on a `sid`, every later sequenced message for that `sid` MUST have `seq == last_seq + 1`.
4. A duplicate, decrease, jump, delta-before-snapshot, unknown `sid`, or impossible book mutation is `GAP`.
5. On `GAP`, atomically mark all books on that `sid` invalid, block entries, close the entire connection, and rebuild all four subscriptions and all six books from a new session.

Do not attempt to trade through a gap. Do not merely clear one market and continue consuming deltas. Do not use ticker data to patch a depth book. Although `get_snapshot` exists, race-free post-gap recovery semantics are not documented; a full reconnect is cheap for six markets and is the required recovery path.

## 5. Orderbook reconstruction

### 5.1 Pin the price convention

The orderbook subscription MUST send `"use_yes_price": true`. Kalshi currently defaults this flag to false but says the default will flip in a future release. With the flag true:

- `side: "yes"` prices remain YES-leg prices.
- `side: "no"` prices are also reported in YES-leg pricing. A NO bid at NO $0.30 is reported at YES $0.70.

Source: [Order direction: orderbook pricing convention](https://docs.kalshi.com/getting_started/order_direction).

Internally maintain, per `market_ticker`:

```ts
type Book = {
  marketId: string;
  yesBids: Map<price_u, qty_u>; // source: side=yes
  yesAsks: Map<price_u, qty_u>; // source: side=no with use_yes_price=true
  snapshotSeen: boolean;
  valid: boolean;
};
```

The `no_dollars_fp` name remains in snapshots even when its prices have been converted to the YES scale. Treat those levels as `yesAsks`, not as NO-price values.

### 5.2 Snapshot

For one `orderbook_snapshot`:

1. Validate `sid`, `seq`, ticker membership, UUID, every decimal string, and duplicate price levels.
2. Parse `yes_dollars_fp` into a new `yesBids` map. Missing means empty.
3. Parse `no_dollars_fp` into a new `yesAsks` map. Missing means empty.
4. Require every quantity to be strictly positive. A zero or duplicate snapshot level is malformed.
5. Replace both maps atomically; never merge a snapshot into the old book.
6. Set `snapshotSeen=true`. Set `valid=true` only after sequence checks pass.

Kalshi documents the snapshot arrays as `[price_in_dollars, contract_count_fp]` and sends a snapshot before deltas. Source: [Orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates).

### 5.3 Delta

`delta_fp` is a signed quantity increment, not an absolute level size. Source: [Orderbook delta schema](https://docs.kalshi.com/websockets/orderbook-updates).

Apply a delta only after its sequence and identity checks pass:

```text
map = yesBids if side == "yes" else yesAsks
old = map.get(price_u, 0)
new = old + delta_qty_u

if new > 0: map[price_u] = new
if new == 0: remove price_u from map
if new < 0: GAP (book invariant failure)
```

The documentation does not separately specify a delete message or explicitly say “remove at zero.” It documents only signed increments. The zero-removal rule is the necessary reconstruction rule and MUST be verified by the create/cancel demo probe before production activation.

### 5.4 Best executable offers

Kalshi books contain bids for the two complementary outcomes; a YES bid at `X` is a NO ask at `1-X`, and a NO bid at `Y` is a YES ask at `1-Y`. Source: [Orderbook Responses](https://docs.kalshi.com/getting_started/orderbook_responses).

With the unified YES-price representation above:

```text
best YES bid = max(yesBids.keys), or None
best YES ask = min(yesAsks.keys), or None

best NO bid  = ONE - best YES ask, or None
best NO ask  = ONE - best YES bid, or None
```

The size at `best YES ask` is `yesAsks[best YES ask]`. The size at `best NO ask` is `yesBids[best YES bid]`.

Never derive a NO ask from the YES ask. A NO ask is derived from the YES **bid**.

### 5.5 Depth at or below a buy limit

For a maximum outcome price `limit_u`:

```text
YES-buy depth(limit_u)
  = sum(qty for yes_price, qty in yesAsks if yes_price <= limit_u)

NO-buy depth(limit_u)
  = sum(qty for yes_price, qty in yesBids if ONE - yes_price <= limit_u)
  = sum(qty for yes_price, qty in yesBids if yes_price >= ONE - limit_u)
```

For a YES buy, consume `yesAsks` from lowest to highest YES price. For a NO buy, consume `yesBids` from highest to lowest YES price; the corresponding NO ask prices `ONE - yes_price` then run from lowest to highest.

If the required opposing map is empty, best offer is `None`, executable depth is zero, and the entry MUST be blocked.

### 5.6 Required invariants

Any violation enters `GAP`:

- A delta arrives before that market's snapshot.
- `market_id` changes for a ticker within a session.
- A parsed price is outside `[0, ONE]`.
- A snapshot quantity is not positive.
- A delta produces a negative level.
- A required numeric string cannot be represented exactly in the configured units.
- `best YES bid >= best YES ask` after applying a message. A transient crossed book may be possible in theory, but Kalshi does not document crossed-book semantics; fail closed and capture the raw frames for the probe.

## 6. State machine

### 6.1 States

| State | Meaning | New entries? |
|---|---|---:|
| `CONNECTING` | TCP/TLS/WebSocket handshake and auth in progress | No |
| `SYNCING` | Connected; subscriptions, snapshots, or REST reconciliation incomplete | No |
| `LIVE` | Every condition in section 6.3 currently holds | **Yes** |
| `STALE` | Connection liveness, clock, or processing-lag threshold failed | No |
| `GAP` | Sequence, schema, identity, or book invariant failed | No |
| `RECONNECTING` | Old session invalidated; backoff/new connection in progress | No |

The entry gate MUST test `state == LIVE` at the final pre-submit check, not only when signal evaluation begins.

### 6.2 Transitions

```text
startup -> CONNECTING
CONNECTING --handshake succeeds--> SYNCING
CONNECTING --failure/timeout--> RECONNECTING

SYNCING --all LIVE predicates true--> LIVE
SYNCING --failure/timeout--> RECONNECTING

LIVE --Ping/clock/processing stale--> STALE
LIVE --seq/schema/book invariant failure--> GAP
LIVE --socket closes/error--> RECONNECTING

STALE -> close socket, invalidate books -> RECONNECTING
GAP   -> close socket, invalidate books -> RECONNECTING
RECONNECTING --backoff elapsed--> CONNECTING
```

Neither `STALE` nor `GAP` may self-heal merely because another valid message arrives.

### 6.3 Exact `LIVE` predicates

All must be true simultaneously:

1. WebSocket is open and authenticated.
2. `orderbook_delta`, `ticker`, `fill`, and `user_orders` each returned `subscribed` with the expected channel and a unique positive `sid`.
3. Every configured market received a valid orderbook snapshot on the current orderbook `sid`.
4. No sequence or book invariant has failed since those snapshots.
5. Last server Ping was received no more than `15_000 ms` ago.
6. Local NTP-reported absolute clock offset is at most `250 ms` (**client policy**).
7. Socket-reader-to-book-apply processing time for the most recent message is at most `1_000 ms`, and no queued message has waited more than `1_000 ms` (**client policy**).
8. For any newly received orderbook/ticker event carrying `ts_ms`, `local_wall_ms - ts_ms <= 2_000 ms` after accounting for measured local clock offset (**client policy**). A violation makes the session stale. Do not require periodic `ts_ms` events from a quiet market.
9. Startup/resume REST reconciliation completed with no unresolved unknown order, fill, position, or risk-state discrepancy.
10. The exchange/market status and the bot's existing risk gates permit entry.

The 15-second Ping threshold is derived from the documented 10-second server Ping interval but is not a Kalshi guarantee. The clock and processing thresholds are application safety choices, not documented protocol limits.

### 6.4 Resume reconciliation order

After every process start or reconnect, keep entries disabled and perform these steps in order:

1. Establish the new WebSocket session and enter `SYNCING`.
2. Subscribe to all four channels and obtain fresh orderbook snapshots for all configured markets.
3. Via REST, verify exchange status and each configured market's tradable status/metadata, including current `price_ranges`.
4. Via REST, fetch all resting orders for the relevant account/subaccount.
5. Match them to the durable local intent ledger by `order_id` and deterministic `client_order_id`. Cancel or explicitly adopt unexpected resting orders; unresolved orders block `LIVE`.
6. Via REST, fetch fills since the last durable reconciliation watermark, paginate to exhaustion, and deduplicate by `trade_id`.
7. Via REST, fetch positions and compare them with positions reconstructed from the durable ledger plus reconciled fills.
8. Via REST, fetch any individual ambiguous order by `order_id` before retrying, canceling, or declaring it absent.
9. Recompute balance, open exposure, concurrency, daily-loss, cooldown, and stop-state gates from reconciled data.
10. Atomically persist the new reconciliation watermark and state. Only then may `SYNCING -> LIVE` occur.

Relevant REST sources: [Get Orders](https://docs.kalshi.com/api-reference/orders/get-orders), [Get Order](https://docs.kalshi.com/api-reference/orders/get-order), [Get Fills](https://docs.kalshi.com/api-reference/portfolio/get-fills), and [Get Positions](https://docs.kalshi.com/api-reference/portfolio/get-positions).

A REST orderbook comparison MAY be logged during resume, but it MUST NOT require exact equality with the WebSocket snapshot because the two requests are not atomic and may observe different instants.

## 7. What REST must still own

WebSocket data is notification and market-state input. It is not the authoritative transaction ledger.

### 7.1 Order submission

Submit orders through REST with a deterministic, durably recorded `client_order_id`. Persist the intent before sending. The authoritative immediate result is the REST response from the create-order endpoint. Source: [Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2).

If the POST times out or its result is otherwise indeterminate, MUST NOT blindly retry with a new ID. Query orders by the known identifiers and reconcile first. A WebSocket `user_order` event may accelerate discovery but may not be the sole proof that the order exists or does not exist.

### 7.2 Cancellation

Cancel through REST. Treat the cancel response and a subsequent REST order query as authoritative. A WebSocket `user_order` canceled event is an optimization/notification, not sufficient proof that no remainder filled before cancellation. Source: [Cancel Order V2](https://docs.kalshi.com/api-reference/orders/cancel-order-v2).

### 7.3 Fills and positions

- WebSocket `fill` is low-latency input only.
- REST `GET /portfolio/fills`, paginated and deduplicated by `trade_id`, owns complete fill reconciliation.
- REST positions own startup/resume position truth.
- `post_position_fp` in one WebSocket fill MUST NOT be treated as proof of complete account state after disconnects or missed messages.

Sources: [User fills](https://docs.kalshi.com/websockets/user-fills), [Get Fills](https://docs.kalshi.com/api-reference/portfolio/get-fills), and [Get Positions](https://docs.kalshi.com/api-reference/portfolio/get-positions).

### 7.4 What must never be inferred from WebSocket alone

- No fill message does not mean no fill.
- No user-order message does not mean an order was not accepted.
- A `canceled` event does not prove zero late/partial fill without fill/order reconciliation.
- A disconnect/reconnect does not replay private history unless explicitly documented; replay is not documented for these channels.
- Ticker best bid/ask is not a replacement for full-depth reconstruction.
- An orderbook snapshot is not the account's order, fill, position, balance, or risk state.

## 8. Logging and acceptance tests

For every inbound frame, durably log before business handling:

```text
session_uuid, recv_wall_ms, recv_monotonic_ns, raw_type, sid, seq,
market_ticker/order_id when present, raw_json, parse_result
```

For every state transition, log old state, new state, reason, session UUID, last Ping age, last sequence by `sid`, queue age, and affected markets.

Production activation requires all of these tests in demo or read-only production:

- Correct RSA-PSS handshake and automatic reconnect.
- One connection carrying all four subscriptions and six markets.
- Fresh snapshot for every book before `LIVE`.
- Replay test proving snapshot plus recorded deltas reproduces a later REST snapshot subject to observation-time races.
- Synthetic dropped-message test proving the next sequence jump blocks entries and reconnects.
- Synthetic delayed-reader test proving a queue age over 1 second blocks entries.
- Missing-Pong test proving the client enters `STALE` at 15 seconds without waiting for server close.
- Process restart with unknown resting order and with an indeterminate POST; both must remain blocked until REST reconciliation.
- Property tests for YES/NO complements and sub-cent prices, including `$0.9660`, without floats.

## 9. Open questions to probe

The following are not fully specified by public documentation.

1. Is `seq` global to an orderbook `sid`, per market, or something else when several markets share one subscription?
2. Can snapshots arrive before the `subscribed` response, and how are multiple initial snapshots interleaved with deltas?
3. Does a `get_snapshot` response establish a race-free new sequence baseline after a gap? This contract does not rely on it.
4. What close delay, close code, and reason result from not answering server Ping frames?
5. Does canceling the last order at a level always produce a signed delta that makes reconstructed size exactly zero?
6. Which fields actually appear on current `fill` and `user_order` messages, given the schema/example contradictions?

### 9.1 Passive wire probe

Save this as `tools/ws_probe.py`. It performs no order writes.

```python
import asyncio
import base64
import json
import os
import time

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

URL = os.getenv(
    "KALSHI_WS_URL",
    "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2",
)
KEY_ID = os.environ["KALSHI_ACCESS_KEY"]
KEY_PATH = os.environ["KALSHI_PRIVATE_KEY_PATH"]
TICKERS = [x for x in os.environ["KALSHI_PROBE_TICKERS"].split(",") if x]
NO_PONG = os.getenv("KALSHI_PROBE_NO_PONG") == "1"


def auth_headers(method: str = "GET", path: str = "/trade-api/ws/v2") -> dict[str, str]:
    timestamp = str(time.time_ns() // 1_000_000)
    with open(KEY_PATH, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    signature = key.sign(
        (timestamp + method + path.split("?", 1)[0]).encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }


async def main() -> None:
    commands = [
        {"id": 1, "cmd": "subscribe", "params": {
            "channels": ["orderbook_delta"],
            "market_tickers": TICKERS,
            "use_yes_price": True,
        }},
        {"id": 2, "cmd": "subscribe", "params": {
            "channels": ["ticker"],
            "market_tickers": TICKERS,
            "send_initial_snapshot": True,
        }},
        {"id": 3, "cmd": "subscribe", "params": {"channels": ["fill"]}},
        {"id": 4, "cmd": "subscribe", "params": {"channels": ["user_orders"]}},
    ]
    last_seq: dict[int, int] = {}
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.ws_connect(
            URL,
            headers=auth_headers(),
            autoping=False,
            heartbeat=None,
        ) as ws:
            for command in commands:
                await ws.send_json(command)
            async for frame in ws:
                recv_wall_ms = time.time_ns() // 1_000_000
                recv_mono_ns = time.monotonic_ns()
                if frame.type == aiohttp.WSMsgType.PING:
                    print(json.dumps({
                        "recv_wall_ms": recv_wall_ms,
                        "recv_monotonic_ns": recv_mono_ns,
                        "control": "ping",
                        "payload_hex": bytes(frame.data).hex(),
                        "pong_sent": not NO_PONG,
                    }), flush=True)
                    if not NO_PONG:
                        await ws.pong(frame.data)
                    continue
                if frame.type == aiohttp.WSMsgType.PONG:
                    print(json.dumps({"recv_wall_ms": recv_wall_ms, "control": "pong"}), flush=True)
                    continue
                if frame.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(frame.data)
                    sid, seq = data.get("sid"), data.get("seq")
                    gap = None
                    if isinstance(sid, int) and isinstance(seq, int):
                        previous = last_seq.get(sid)
                        if previous is not None and seq != previous + 1:
                            gap = {"previous": previous, "current": seq}
                        last_seq[sid] = seq
                    print(json.dumps({
                        "recv_wall_ms": recv_wall_ms,
                        "recv_monotonic_ns": recv_mono_ns,
                        "gap_by_sid_rule": gap,
                        "wire": data,
                    }, separators=(",", ":")), flush=True)
                    continue
                print(json.dumps({
                    "recv_wall_ms": recv_wall_ms,
                    "frame_type": str(frame.type),
                    "data": str(frame.data),
                }), flush=True)


asyncio.run(main())
```

Install and run it against two actively changing markets:

```bash
python3 -m venv .venv-ws-probe
.venv-ws-probe/bin/pip install aiohttp cryptography
export KALSHI_ACCESS_KEY='your-demo-key-id'
export KALSHI_PRIVATE_KEY_PATH='/absolute/path/to/demo-private-key.pem'
export KALSHI_PROBE_TICKERS='ACTIVE-MARKET-1,ACTIVE-MARKET-2'
timeout 180 .venv-ws-probe/bin/python tools/ws_probe.py | tee ws-probe.jsonl
```

On macOS, where coreutils `timeout` may be absent, run the last command directly and stop it with Ctrl-C after three minutes.

Evaluate sequence scope and ordering:

```bash
jq -r 'select(.wire.seq != null) | [.wire.sid,.wire.seq,.wire.type,.wire.msg.market_ticker] | @tsv' ws-probe.jsonl
jq -c 'select(.gap_by_sid_rule != null)' ws-probe.jsonl
```

If sequences are consecutive by `sid` while tickers interleave, the contract's provisional rule is confirmed. If not, keep production blocked and determine the actual grouping from the trace; do not relax gap detection from one short sample.

Probe missing-Pong behavior in demo only:

```bash
KALSHI_PROBE_NO_PONG=1 timeout 90 .venv-ws-probe/bin/python tools/ws_probe.py | tee ws-no-pong.jsonl
```

Record elapsed time from the last Ping to close plus the final frame/close code. Regardless of the result, retain the client's 15-second fail-closed timer.

### 9.2 Demo create/cancel probe

While `ws_probe.py` is running against demo, save the following as `tools/demo_order_probe.py`. It creates one **post-only demo order** and cancels it. It is hard-blocked from using a non-demo host. Choose a valid, non-marketable price from the demo book; if it would cross, `post_only` should prevent execution.

```python
import base64
import json
import os
import time
import uuid
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASE = os.getenv("KALSHI_REST_URL", "https://external-api.demo.kalshi.co/trade-api/v2")
if ".demo." not in BASE and "demo-api" not in BASE:
    raise SystemExit("Refusing to place probe order outside Kalshi demo")
KEY_ID = os.environ["KALSHI_ACCESS_KEY"]
KEY_PATH = os.environ["KALSHI_PRIVATE_KEY_PATH"]
TICKER = os.environ["KALSHI_PROBE_ORDER_TICKER"]
PRICE = os.environ["KALSHI_PROBE_ORDER_PRICE"]  # e.g. 0.0100; must be valid/non-marketable

with open(KEY_PATH, "rb") as f:
    KEY = serialization.load_pem_private_key(f.read(), password=None)


def headers(method: str, path: str) -> dict[str, str]:
    timestamp = str(time.time_ns() // 1_000_000)
    signature = KEY.sign(
        (timestamp + method + path.split("?", 1)[0]).encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }


def call(method: str, path: str, body=None):
    raw = None if body is None else json.dumps(body).encode()
    request = Request(BASE + path.removeprefix("/trade-api/v2"), data=raw,
                      headers=headers(method, path), method=method)
    with urlopen(request, timeout=10) as response:
        return json.load(response)


create_path = "/trade-api/v2/portfolio/events/orders"
created = call("POST", create_path, {
    "ticker": TICKER,
    "client_order_id": str(uuid.uuid4()),
    "side": "bid",
    "count": "1.00",
    "price": PRICE,
    "time_in_force": "good_till_canceled",
    "self_trade_prevention_type": "taker_at_cross",
    "post_only": True,
    "cancel_order_on_pause": True,
})
print(json.dumps({"created": created}, indent=2))
order_id = created["order_id"]
cancel_path = f"/trade-api/v2/portfolio/events/orders/{order_id}"
cancelled = call("DELETE", cancel_path)
print(json.dumps({"cancelled": cancelled}, indent=2))
```

Run:

```bash
export KALSHI_PROBE_ORDER_TICKER='ACTIVE-DEMO-MARKET'
export KALSHI_PROBE_ORDER_PRICE='0.0100'
.venv-ws-probe/bin/python tools/demo_order_probe.py
```

Inspect the corresponding `user_order` create/cancel events and the own-order `orderbook_delta` events in `ws-probe.jsonl`. Confirm field presence, `client_order_id`, signed increment behavior, and exact zero removal. To resolve the `fill` shape inconsistency, repeat in demo with a deliberately marketable one-contract order using a price taken from the current demo book; do not run that probe against production.

---

## Appendix A — empirical validation, 2026-08-22

Probed against the live production feed before any of this was built on. Read-only:
subscribe, observe, disconnect. No orders, no state change.

**Confirmed against `KXBTC15M-26AUG221415-15`:**

| claim | result |
|---|---|
| `wss://external-api-ws.kalshi.com/trade-api/ws/v2` | CONNECTED |
| Signature message `<ts>GET/trade-api/ws/v2`, RSA-PSS-SHA256, headers on upgrade | ACCEPTED |
| `subscribe` shape, `use_yes_price` param | `{"type":"subscribed","id":1,"msg":{"channel":"orderbook_delta","sid":1}}` |
| Snapshot fields `yes_dollars_fp` / `no_dollars_fp` | EXACT |
| Snapshot pair shape `[price_dollars, count_fp]` as **strings** | `['0.0010', '407266.00']` |
| Delta fields `price_dollars` / `delta_fp` / `side` / `ts_ms` | EXACT |
| `seq` monotonic from 1 within a `sid` | 1,2,3…13 observed unbroken |
| 4-dp decimal prices, never floats | `'0.9400'`, `delta_fp` `'8.27'` |

Sample delta, verbatim:

```json
{"market_ticker":"KXBTC15M-26AUG221415-15",
 "market_id":"29c871ed-e7bf-4377-a131-427c815797b8",
 "price_dollars":"0.9400","delta_fp":"8.27","side":"no",
 "ts":"2026-08-22T18:01:26.758621Z","ts_ms":1787421686758}
```

Note `delta_fp` of `8.27` — a fractional contract count, which is exactly why §3.1's
"MUST NOT parse through IEEE-754" is load-bearing rather than pedantic. This codebase
was already bitten once by sub-cent quoting (`96.6000c` broke a `\d+` regex on
2026-08-21).

### `use_yes_price` — RESOLVED, honoured

A/B subscribe on the same market, same minute, snapshot only:

```
use_yes_price=True    no_dollars_fp:  71.0 .. 99.9    (YES scale)
use_yes_price=False   no_dollars_fp:   0.1 .. 29.0    (NO scale)
```

The flag is honoured. §5.1 stands, and the `no_dollars_fp` name does keep its
NO-side meaning while carrying YES-scale prices, exactly as §5.1 warns.

### Snapshot reconstruction — CONFIRMED to within one tick

With the flag on, `max(yes_dollars_fp)`=70.0c and `min(no_dollars_fp)`=71.0c against a
REST `yes_bid`/`yes_ask` of 69/70 — a one-tick move between the two calls, in a market
seconds from close. §5.2 and §5.4 are sound.

### DELTA APPLICATION — VALIDATED. My earlier conclusion here was WRONG.

This section previously said the delta path was broken and must not be built on. That
was a broken TEST, not a broken implementation, and the error was mine.

The 13c-34c divergence came from comparing the WS book against `/markets/{ticker}` — a
summary quote that is neither atomic with the book nor current. The correct oracle is
`/markets/{ticker}/orderbook`. Measured skew: WS runs 1-31 sequence messages and
280-1350ms ahead of the REST summary, which is the entire discrepancy.

A 140s single-market run against the correct oracle produced **14 consecutive exact
full-book matches** — every level, every quantity:

```
RACE_EXACT t=10.3s  seq=475->482   matched WS seq=475  seq_lag=7   recv_lag_ms=348.3
OK_EXACT   t=50.3s  seq=1984       full_book_exact=1
RACE_EXACT t=140.3s seq=3087->3110 matched WS seq=3087 seq_lag=23  recv_lag_ms=635.5
```

So the following are confirmed correct, not merely plausible: signed incremental
`delta_fp`, zero-removal, and mapping `side: "no"` to `yesAsks` under
`use_yes_price: true`.

**The tell I misread:** two of six markets matched in the bad run. I read that as a
race in snapshot ordering. The real reason is that quiet markets do not move in 300ms,
so their tops agreed by luck while active markets diverged. A correct-looking subset
is not evidence of a correct implementation.

### Two corrections to §4 and §5.3 that the validation forced

- **`seq` is contiguous per `sid`, not per ticker.** One missing message invalidates
  every market on that subscription. Track `last_seq_by_sid[sid]`, never per-ticker
  counters. Not documented; established from production traffic.
- **`new < 0` must enter GAP, not remove the level.** Collapsing `new <= 0` into a
  pop silently masks corruption — the failure mode where the book stays plausible while
  being wrong. Exact zero removes; negative is an impossible state.

### NEW open question: spontaneous re-snapshot

The same run ended with:

```
STATE LIVE -> GAP: unexpected second snapshot without an explicit resync
FAIL CLOSED
```

Kalshi sent a second `orderbook_snapshot` on a live subscription without being asked.
The harness treats that as a gap and fails closed, which is the safe default but would
force a reconnect every time it happens.

Likely benign: the subscribed market was `KXETH15M-26AUG221545-45`, closing 15:45 ET,
and the run spanned that close. Market rollover is the obvious candidate. **Not
confirmed** — before running a WS client in production, watch a market that is NOT near
close for several minutes and see whether the second snapshot still arrives. If it does,
re-snapshot is routine and §6 needs a RESYNC transition rather than a GAP.

**Still unverified:****Still unverified:**

- Zero-removal on delta (§5.3) — not documented; needs the create/cancel probe.
- The delta path as a whole; see above.
- Pong deadline and close code on keepalive violation (§1.3).
- `fill` and `user_orders` shapes — not exercised, since probing them means placing an
  order.

Reproduce: `scratchpad/wsprobe.py` (read-only, ~25s).
