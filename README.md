# ParametricInsurance

A GenLayer Intelligent Contract primitive for **parametric insurance**: a product covers a measurable, indexable real-world parameter (flight-delay minutes, rainfall mm, temperature, ...) and pays out automatically when that parameter crosses a configured trigger threshold — with the trigger value verified by a **multi-source oracle consensus** rather than a single point of failure.

An insurer deploys one or more `Product`s, holders buy `Policy`s by paying the exact `premium`, and covered claims are paid straight to the holder via a native `emit_transfer`. No LLM decides how much money moves — the premium and coverage are fixed per product, and the only non-deterministic step is *reading the parameter from real data sources*.

## Why this is more than "AI decides X"

| Concern | How it's handled |
|---|---|
| Real money flow | `buy_policy` is a **payable** write that requires the *exact* `premium` (`gl.message.value`). Approved claims pay the fixed `coverage` straight to the holder's EOA via `_EOA(holder).emit_transfer(...)`, where `_EOA` is an `@gl.evm.contract_interface` — value transfers to non-contract addresses go through the ghost contract (external message), per the GenLayer "Value Transfers" docs. The payout pool is funded with `fund_pool` and only product creators can `withdraw_funds` (capped at `self.balance`). A covered claim reverts with a clear message when the pool is empty. Note that `emit_transfer(on="finalized")` settles asynchronously: the sender balance drops only when the external message finalizes, so integration tests poll for the settled balance. |
| Multi-source oracle consensus | A claim must be backed by data fetched from at least `MIN_DATA_SOURCES` (2) *independent* URLs configured on the product. The non-deterministic block only **acquires** each source (`gl.nondet.web.get`) and **extracts** a raw integer reading (`gl.nondet.exec_prompt`) — nothing else. `_consensus_validator` runs on every validator, which **independently re-acquires and re-extracts**, and only accepts the leader's readings if (a) it saw exactly the same set of reachable sources, (b) each source reading agrees within a **tolerance band**, and (c) the deterministic median of the leader's readings and its own lie on the **same side of the payout threshold**. |
| Tolerance band + same-threshold-outcome guard | `tolerance_pct` is interpreted as a % of `threshold` (not of the measured values), so disagreement is measured in the units that matter for the payout decision — a 10% band on a 180-minute threshold means sources must agree within ±18 minutes. Because that band can in principle span the trigger, the validator *also* requires that the median implied by its own independent readings crosses the threshold in the **same direction** as the leader's. Reading on opposite sides of the threshold is always rejected, even when it would fit the band. |
| Payout decision is deterministic and integer-only | The block only produces **raw per-source readings**. The median (`agreed_value`), the threshold crossing, and the payout amount are plain integer arithmetic computed *outside* the consensus block in `file_claim`. There is no `float()` anywhere — GenVM lint treats floats as a non-deterministic pattern, so the whole pipeline (LLM value parse, median, tolerance band, threshold decision, payout) is integer math. |
| Fraud / abuse resistance | Only the policy holder can file a claim; a policy can be claimed at most once; claims are rejected (never paid) when sources can't reach consensus or when fewer than `MIN_DATA_SOURCES` sources are reachable; suspended products can't issue policies or pay claims; `withdraw_funds` is role-gated and balance-capped. |
| Deterministic, unit-testable helpers | `_parse_number`, `_median`, `_tolerance_band`, `_within_tolerance`, `_strip_code_fence`, `_parse_json_object`, `_consensus_ok` are plain, integer-only Python tested in `tests/direct/test_helpers.py` with no VM. |

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
      ├── consensus block (nondet, minimal): fetch each data_sources URL
      │     + extract a raw integer reading per source (leader + validators
      │     independently); median/threshold/payout computed after
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
    leader_fn,     # def leader_fn():      -> _acquire_extract(data_sources, parameter_name, event_context)
    validator_fn,  # def validator_fn(r):  -> _consensus_validator(r, data_sources, parameter_name,
                   #                          event_context, tolerance_pct, threshold)
)
```

The non-deterministic block is intentionally **minimal** — it only *acquires* and *extracts*: for each URL, `gl.nondet.web.get(url)` then `gl.nondet.exec_prompt(...)` to read the parameter out of the page for the event under review. No median, no tolerance band, no threshold math run inside the block. Unreachable/unparseable sources are skipped; with fewer than `MIN_DATA_SOURCES` surviving sources the block reports `ok=False` (→ claim rejected, never paid).

- **Leader** (`_acquire_extract`): returns only the *raw* per-source integer readings `{"ok": True, "values": {url: int}}` — aggregation is deliberately left to deterministic code.
- **Validator** (`_consensus_validator`): re-runs the acquisition + extraction independently (`run_nondet_unsafe` re-executes the block on every validator), then requires:
  1. an **exact match on the set of reachable sources**;
  2. every source value to agree within the tolerance band (`tolerance_pct` % of `threshold`, integer math);
  3. the **median of its own readings and the leader's median to lie on the same side of `threshold`** — a reading that would flip the payout outcome is always rejected, so the tolerance band can never approve readings on opposite sides of the trigger.

Both functions are plain named functions passed to `run_nondet_unsafe` (not lambdas) — an inter-contract call graph that GenVM lint treats as a safe scope, matching the accepted `content_quality_assessor` contract. The median `agreed_value`, the threshold comparison, and the payout are computed **outside** the block in `file_claim`, in plain integer arithmetic.

This is genuine multi-source consensus with a real equivalence check — not a thin "AI says yes/no" wrapper, and not a single-API oracle. The failure mode is biased toward *not paying* (a claim is rejected if even one validator sees divergent data, a different source set, or a different threshold outcome).

## Deployed instance (GenLayer Studio)

Latest clean deployment of `parametric_insurance.py` (corrected source: minimal non-deterministic block + same-threshold-outcome validator guard, `# v0.3.0-rc7` runner version line). Verified with `genvm-lint check` (lint + SDK validation, exit 0), 57 direct tests, and the write-method integration suite (9 passed) against the deployed address below:

| Network | Address | Explorer |
|---|---|---|
| Studio | `0xD56ff0EE9329b06413C9B097dcA2Bf0b923d3Aad` | [View on Explorer](https://explorer-studio.genlayer.com/address/0xD56ff0EE9329b06413C9B097dcA2Bf0b923d3Aad) |

> `emit_transfer` to an EOA settles at finalization of the external message, not at transaction acceptance. Integration tests therefore poll `get_pool_balance` until the settled value is observed. Write transactions against Studio are also rate-limited (30 req/min), so the integration suite throttles JSON-RPC calls and retries connection/rate-limit errors (see `tests/integration/conftest.py`).

## Testing

- `tests/direct/test_helpers.py` — pure-Python unit tests of the deterministic helpers, loaded with a tiny `genlayer` stub (no Studio, no network): `pytest tests/direct/test_helpers.py`
- `tests/direct/test_parametric_insurance.py` — direct-mode tests (no server) for the full lifecycle, money flow (premium → pool → payout via a value-transfer hook), consensus behavior (agree / disagree / insufficient sources / threshold-straddle rejection, exercised with `direct_vm.run_validator()`), and access control: `pytest tests/direct/test_parametric_insurance.py`
- Security/edge cases (in the two files above, counted in the 57): integer-parse edge cases (`.5`, `1e21`, `inf`, `-0`, giant ints), tolerance-band boundary inclusivity and negative-input clamping, source-set dedupe / min-max cap, `fund_pool`/`withdraw` zero-value reverts, garbage-extraction fail-safe (never pays), and the documented permissionless-creator shared-pool-withdraw behavior
- `tests/integration/test_probe_deployed.py` — read-only probe of the deployed contract (counts, balances) bound to `DEPLOYED_ADDRESS`.
- `tests/integration/test_deployed_contract.py` — full write lifecycle against the deployed contract (create product → buy policy with exact premium → wrong-premium/unknown-product reverts → fund pool → withdraw → suspend product → file claim & second-claim revert → create-product validation reverts): `pytest tests/integration/test_deployed_contract.py`. Ids derive from on-chain counters, so the suite is rerunnable against any fresh deployment by changing `DEPLOYED_ADDRESS`.

Test-runner notes (Windows, from the sibling contracts):
- The gltest plugin resolves every env var referenced by `gltest.config.yaml` at `pytest_configure`, so `PRIVATE_KEY_1`, `PRIVATE_KEY_2` and `TESTNET_PRIVATE_KEY` must be set (even for direct tests that never touch the network). Dummy 64-hex values work for direct runs.
- Direct mode downloads the pinned `py-genlayer` runner into `~/.cache/gltest-direct`. On Windows set `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE` to a certifi bundle if certificate verification fails.

## Known limitations (by design)

- The product creator is trusted to configure honest `data_sources`. The contract verifies agreement *between* sources, not the truth of the sources themselves — a product whose sources all feed the same upstream is a single point of failure.
- Coverage is a fixed per-policy amount; there is no scaling by `agreed_value` (e.g. no "payout = delay × rate"). That's a deliberate simplification — the fixed-amount design keeps the payout decision fully deterministic.
- No claim-settlement period / adjudication contest: a claim is paid in the same transaction that reaches consensus. A composing contract could add a dispute window on top.
- `withdraw_funds` is a flat pool withdrawal shared across all products of a contract; per-product accounting is left to the insurer. **Security consequence:** `create_product` is permissionless, so any EOA can become a "creator" and withdraw the shared pool's liquidity funded by others. The behavior is pinned by `test_any_creator_can_withdraw_shared_pool`. Fixing it (per-creator accounting or deployer-only creation) requires a redesign + redeploy.
- The model is a shared oracle: `event_context` is interpolated into the extraction prompt, so a holder's prompt-injection could steer all readings (validators use the same model, so multi-source only guarantees *consistency*, not *truth* of the sources).
- `emit_transfer` settles at finalization, so a claim checks `self.balance` before the transfer lands; a creator withdraw executed before that settlement could leave a pending payout short. Low risk in the testnet context.

## Design lesson: anchor the tolerance band to the trigger — and never let it straddle it

Parametric payouts hinge on the boundary `agreed_value >= threshold`, so that is the region where source disagreement matters most. Interpreting `tolerance_pct` as a percentage of `threshold` (rather than of the measured magnitudes) keeps the band meaningful both near the trigger (where it decides payouts) and far from it (where tiny absolute disagreements like 0 vs 5 minutes should not fail a claim on a 180-minute threshold). It also stays conservative at the top of the range: sources reading 1000 vs 1050 minutes fail a 10% band on a 180-minute threshold, even though they'd pass a 10% band on 1000.

A tolerancer band can *still* span the threshold (e.g. a 15% band on threshold 180 is ±27, so readings 180 vs 205 are "close" but payout on opposite sides). That is why the validator does not stop at per-source tolerance: it additionally requires its own independent median to cross the threshold in the same direction as the leader's. The equivalence check therefore preserves the **payout decision itself**, not just the raw numbers — the reviewer's rejection ("the validator's tolerance can approve readings on opposite sides of the payout threshold") is addressed by the same-threshold-outcome guard in `_consensus_validator`.

## Dependency pin

The contract pins `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` in its `Depends` header. Update this hash if you're targeting a different SDK version.
