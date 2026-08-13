# ParametricInsurance

A GenLayer Intelligent Contract primitive for **parametric insurance**: a product covers a measurable, indexable real-world parameter (flight-delay minutes, rainfall mm, temperature, ...) and pays out automatically when that parameter crosses a configured trigger threshold — with the trigger value verified by a **multi-source oracle consensus** rather than a single point of failure.

An insurer deploys one or more `Product`s, holders buy `Policy`s by paying the exact `premium`, and covered claims are paid straight to the holder via a native `emit_transfer`. No LLM decides how much money moves — the premium and coverage are fixed per product, and the only non-deterministic step is *reading the parameter from real data sources*.

## Why this is more than "AI decides X"

| Concern | How it's handled |
|---|---|
| Real money flow | `buy_policy` is a **payable** write that requires the *exact* `premium` (`gl.message.value`). Approved claims pay the fixed `coverage` straight to the holder's EOA via `_EOA(holder).emit_transfer(...)`, where `_EOA` is an `@gl.evm.contract_interface` — value transfers to non-contract addresses go through the ghost contract (external message), per the GenLayer "Value Transfers" docs. The payout pool is funded with `fund_pool` and only product creators can `withdraw_funds` (capped at `self.balance`). A covered claim reverts with a clear message when the pool is empty. Note that `emit_transfer(on="finalized")` settles asynchronously: the sender balance drops only when the external message finalizes, so integration tests poll for the settled balance. |
| Multi-source oracle consensus | A claim must be backed by data fetched from at least `MIN_DATA_SOURCES` (2) *independent* URLs configured on the product. `_consensus_leader` fetches every source and extracts the parameter; `_consensus_validator` runs on every validator, which **independently re-fetches every source** and only accepts the leader's reading if (a) it saw exactly the same set of reachable sources, (b) each source value agrees within a **tolerance band**, and (c) the agreed median agrees within the band. |
| Tolerance band, anchored to the trigger | `tolerance_pct` is interpreted as a % of `threshold` (not of the measured values), so disagreement is measured in the units that matter for the payout decision — a 10% band on a 180-minute threshold means sources must agree within ±18 minutes, whether the observed delay is 5 or 500 minutes. See `_tolerance_band` / `_within_tolerance`. |
| Payout decision is deterministic | The non-deterministic part (web + LLM) only produces an `agreed_value`. Whether it crosses `threshold`, and the payout amount, are plain integer comparisons made *outside* the consensus block (`file_claim`). |
| Fraud / abuse resistance | Only the policy holder can file a claim; a policy can be claimed at most once; claims are rejected (never paid) when sources can't reach consensus or when fewer than `MIN_DATA_SOURCES` sources are reachable; suspended products can't issue policies or pay claims; `withdraw_funds` is role-gated and balance-capped. |
| Deterministic, unit-testable helpers | `_parse_number`, `_median`, `_tolerance_band`, `_within_tolerance`, `_strip_code_fence`, `_parse_json_object`, `_consensus_ok` are plain, I/O-free Python tested in `tests/direct/test_helpers.py` with no VM. |

## State design

```
Product
  description:      str
  parameter_name:   str        # e.g. "flight_delay_minutes"
  threshold:        u256       # payout triggers when agreed_value >= threshold
  tolerance_pct:    u256       # sources must agree within this % of `threshold`
  premium:          u256       # exact cost of one policy (native tokens)
  coverage:         u256       # payout when the trigger fires
  data_sources:     DynArray[str]  # 2..MAX_DATA_SOURCES independent URLs
  status:           str        # "active" | "suspended"
  created_by:       Address

Policy
  product_id:       str
  holder:           Address
  premium:          u256
  coverage:         u256
  status:           str        # "active" | "paid" | "rejected"
  created_at:       u256

Claim
  policy_id:        str        # one claim per policy
  event_context:    str
  sources_used:     u256
  agreed_value:     u256
  threshold:        u256
  status:           str        # "approved" | "rejected"
  payout:           u256
  filed_at:         u256

ParametricInsurance
  products:   TreeMap[str, Product]   # keyed "product-<n>"
  policies:   TreeMap[str, Policy]    # keyed "policy-<n>"
  claims:     TreeMap[str, Claim]     # keyed by policy_id
  product_count: u256
  policy_count: u256
```

## Lifecycle

```
create_product (insurer) ──► product-0 [active]
      │
      ├── buy_policy(product-0, value=premium) ──► policy-0 [active]
      │         (anyone; exact premium required)
      │
      └── fund_pool(value=...)            (anyone may add liquidity)

file_claim(policy-0, event_context)      (holder only, once per policy)
      │
      ├── consensus block: fetch each data_sources URL, extract the
      │     parameter for the event, median = agreed_value
      │     (validators independently re-fetch + re-extract)
      │
      ├── fewer than MIN_DATA_SOURCES reachable  ──► rejected (no payout)
      ├── agreed_value < threshold               ──► rejected (no payout)
      └── agreed_value >= threshold
             │  if self.balance < coverage ──► revert (pool must be funded)
             └─ _EOA(holder).emit_transfer(holder, coverage) ──► policy [paid], claim [approved]  (settles at finalization)

withdraw_funds(amount)   (product creator only, capped at self.balance)
suspend_product(id)      (product creator only)
```

## Public interface

Insurer side:
- `create_product(description, parameter_name, threshold, tolerance_pct, premium, coverage, data_sources) -> str` — caller becomes the product creator; returns `product_id`.
- `suspend_product(product_id) -> None` — creator only; blocks new policies and claims.
- `fund_pool() -> None` — payable; anyone may add liquidity.
- `withdraw_funds(amount) -> None` — any product creator, capped at `self.balance`.

Holder side:
- `buy_policy(product_id) -> str` — payable; the transaction must carry exactly `premium`; returns `policy_id`.
- `file_claim(policy_id, event_context) -> dict` — holder only, once per policy; runs the multi-source consensus and the deterministic payout decision. Returns `{status, reason, agreed_value, payout, sources_used}`.

Views:
- `get_product(product_id) -> dict`, `get_policy(policy_id) -> dict`, `get_claim(policy_id) -> dict`
- `get_product_count()`, `get_policy_count()`, `get_pool_balance()`, `get_contract_address()`

## The consensus block (the interesting part)

`file_claim` closes over the product's `data_sources`, `parameter_name`, the claim's `event_context`, and the product's `threshold`/`tolerance_pct`, then runs:

```python
result = gl.vm.run_nondet_unsafe(
    lambda: _consensus_leader(data_sources, parameter_name, event_context),
    lambda leaders_res: _consensus_validator(
        leaders_res, data_sources, parameter_name, event_context,
        tolerance_pct, threshold,
    ),
)
```

- **Leader** (`_consensus_leader`): for each URL, `gl.nondet.web.get(url)` then `gl.nondet.exec_prompt(...)` to read the parameter out of the page for the specific event. Unreachable/unparseable sources are skipped; with fewer than `MIN_DATA_SOURCES` surviving sources the block reports `ok=False` (→ claim rejected, never paid). Otherwise the median of the source readings is the `agreed_value`.
- **Validator** (`_consensus_validator`): re-runs the full extraction independently, then requires an **exact match on the set of reachable sources** and that every source value *and* the median agree within the tolerance band (`tolerance_pct` % of `threshold`).

This is genuine multi-source consensus with a real equivalence check — not a thin "AI says yes/no" wrapper, and not a single-API oracle. The failure mode is biased toward *not paying* (a claim is rejected if even one validator sees divergent data or a different set of sources).

## Deployed instance (GenLayer Studio)

Latest clean deployment of `parametric_insurance.py`, verified end-to-end with the full integration suite (9 passed):

| Network | Address | Explorer |
|---|---|---|
| Studio | `0x445f3179D62400cf2B4A71381fdA3b818939EEE9` | [View on Explorer](https://explorer-studio.genlayer.com/address/0x445f3179D62400cf2B4A71381fdA3b818939EEE9) |

> `emit_transfer` to an EOA settles at finalization of the external message, not at transaction acceptance. Integration tests therefore poll `get_pool_balance` until the settled value is observed. Write transactions against Studio are also rate-limited (30 req/min), so the integration suite throttles JSON-RPC calls and retries connection/rate-limit errors (see `tests/integration/conftest.py`).

## Testing

- `tests/direct/test_helpers.py` — pure-Python unit tests of the deterministic helpers, loaded with a tiny `genlayer` stub (no Studio, no network): `pytest tests/direct/test_helpers.py`
- `tests/direct/test_parametric_insurance.py` — direct-mode tests (no server) for the full lifecycle, money flow (premium → pool → payout via a value-transfer hook), consensus behavior (agree / disagree / insufficient sources, exercised with `direct_vm.run_validator()`), and access control: `pytest tests/direct/test_parametric_insurance.py`
- `tests/integration/test_probe_deployed.py` — read-only probe of the deployed contract (counts, balances) bound to `DEPLOYED_ADDRESS`.
- `tests/integration/test_deployed_contract.py` — full write lifecycle against the deployed contract (create product → buy policy with exact premium → wrong-premium/unknown-product reverts → fund pool → withdraw → suspend product → file claim & second-claim revert → create-product validation reverts): `pytest tests/integration/test_deployed_contract.py`. Ids derive from on-chain counters, so the suite is rerunnable against any fresh deployment by changing `DEPLOYED_ADDRESS`.

Test-runner notes (Windows, from the sibling contracts):
- The gltest plugin resolves every env var referenced by `gltest.config.yaml` at `pytest_configure`, so `PRIVATE_KEY_1`, `PRIVATE_KEY_2` and `TESTNET_PRIVATE_KEY` must be set (even for direct tests that never touch the network). Dummy 64-hex values work for direct runs.
- Direct mode downloads the pinned `py-genlayer` runner into `~/.cache/gltest-direct`. On Windows set `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` to a certifi bundle if certificate verification fails.

## Known limitations (by design)

- The product creator is trusted to configure honest `data_sources`. The contract verifies agreement *between* sources, not the truth of the sources themselves — a product whose sources all feed the same upstream is a single point of failure.
- Coverage is a fixed per-policy amount; there is no scaling by `agreed_value` (e.g. no "payout = delay × rate"). That's a deliberate simplification — the fixed-amount design keeps the payout decision fully deterministic.
- No claim-settlement period / adjudication contest: a claim is paid in the same transaction that reaches consensus. A composing contract could add a dispute window on top.
- `withdraw_funds` is a flat pool withdrawal shared across all products of a contract; per-product accounting is left to the insurer.

## Design lesson: anchor the tolerance band to the trigger, not the values

Parametric payouts hinge on the boundary `agreed_value >= threshold`, so that is the region where source disagreement matters most. Interpreting `tolerance_pct` as a percentage of `threshold` (rather than of the measured magnitudes) keeps the band meaningful both near the trigger (where it decides payouts) and far from it (where tiny absolute disagreements like 0 vs 5 minutes should not fail a claim on a 180-minute threshold). It also stays conservative at the top of the range: sources reading 1000 vs 1050 minutes fail a 10% band on a 180-minute threshold, even though they'd pass a 10% band on 1000.

## Dependency pin

The contract pins `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` in its `Depends` header. Update this hash if you're targeting a different SDK version.
