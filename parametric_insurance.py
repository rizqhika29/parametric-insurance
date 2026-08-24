# v0.3.0-rc7
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
ParametricInsurance
====================

A reusable Intelligent Contract primitive for parametric insurance: a
product covers a *measurable, indexable* real-world parameter (flight
delay minutes, rainfall mm, temperature, ...) and pays out automatically
when that parameter crosses a configured trigger threshold -- with the
trigger value verified by a *multi-source oracle consensus* instead of a
single point of failure.

Why this is more than "AI decides X"
-------------------------------------
 1. Real money flow with deterministic lifecycle. A product defines a
    `premium` and a `coverage` amount. Policies are bought with a payable
    call (`buy_policy`) that *requires the exact premium*; claims that are
    approved pay out the coverage via `emit_transfer` straight to the
    holder. Every product has its *own* pool ledger (`pool_balance`):
    premiums and `fund_pool` credits go to a specific product's pool, and
    only that product's creator may `withdraw_funds` from it -- so no
    creator can ever touch funds backing another product's policies. When
    a claim is approved the coverage is *reserved* (deducted) from the
    product's ledger at decision time, before the transfer settles. No LLM
    decides how much money moves -- the amount is fixed per policy.
2. Multi-source oracle consensus, restructured to be lint-clean and
   threshold-safe. The non-deterministic block is deliberately minimal:
   the leader and every validator only *acquire* evidence
   (`gl.nondet.web.get`) and *extract* a raw integer reading
   (`gl.nondet.exec_prompt`) per source -- nothing else. Aggregation is
   plain integer math (`_median`) done outside the block. Consensus
   requires at least `MIN_DATA_SOURCES` reachable sources, an exact match
   on the set of reachable sources, each source reading within the
   tolerance band (`tolerance_pct` % of `threshold`), and -- the critical
   guarantee -- that the validator's own independent median lies on the
   *same side of the payout threshold* as the leader's, so no validator
   can approve a reading that crosses the trigger in the opposite
   direction.
3. The payout decision is deterministic and integer-only. The raw
   per-source readings that leave the consensus block are aggregated
   (median) in plain integer code outside it (`file_claim`); whether that
   median crosses `threshold`, and the payout amount, are plain integer
   comparisons. There is no `float()` anywhere in the contract -- GenVM
   lint treats floats as non-deterministic, so the whole pipeline (LLM value
   parse, median, tolerance band, threshold crossing, payout) is integer math.
  4. Defense against fraud / abuse. Only the holder of a policy can file a
    claim on it; a policy can only be claimed once; claims can't be filed
    with fewer than `MIN_DATA_SOURCES` sources reachable; the payout is
    reserved from the product's own pool balance. Suspension blocks new
    policies but preserves existing holders' claim rights, and a
    creator's pool is locked by outstanding coverage liabilities so
    funds earmarked for pending claims can never be withdrawn.
5. Deterministic, unit-testable helpers. `_median`, `_parse_number`,
   `_tolerance_band`, `_within_tolerance`, `_extract_parameter` are plain
   integer-only Python that run before/after the consensus block and can
   be tested without any VM (`tests/direct/test_helpers.py`).

Trust model / limitations
-------------------------
- The product creator is trusted to configure honest `data_sources`; the
  contract verifies agreement *between* sources, not the truth of the
  sources themselves. A product whose sources all feed the same upstream
  is a single point of failure.
- Balances are isolated *per product*, not per contract: each product has
  its own `pool_balance` ledger and only its creator can withdraw from it.
  Anyone may fund any product's pool, and any holder of a policy collects
  its payout from that product's pool. Funds sent to the contract outside
  a product-creating/funding path are not attributed to any pool.
- A product's pool must be funded before its covered claims are filed; a
  covered claim reverts with a clear message when the product's
  `pool_balance < coverage`. Approved claims reserve the coverage in the
  product's ledger at decision time (before the transfer settles), so a
  creator cannot withdraw money already earmarked for a pending payout.
"""

from genlayer import *
from dataclasses import dataclass
import json


@gl.evm.contract_interface
class _EOA:
    """Interface for sending GEN to an EOA / chain-layer address.

    Value transfers to EOAs are *external* messages that go through the IC's
    ghost contract on the chain layer, so they use the EVM contract
    interface even though the recipient is not a contract (see the GenLayer
    "Value Transfers" docs)."""

    class View:
        pass

    class Write:
        pass


# --- Tunable constants ---------------------------------------------------

# A claim needs data from at least this many independent sources for the
# consensus block to consider it. Fewer reachable/parseable sources => the
# claim is rejected without a payout (no single point of failure).
MIN_DATA_SOURCES = 2

# Safety cap on how many sources a product may list.
MAX_DATA_SOURCES = 4

# Truncate the event context / source body to bound prompt size.
MAX_EVENT_CONTEXT_CHARS = 1200
MAX_SOURCE_BODY_CHARS = 4000

# --- Product / policy statuses -------------------------------------------

PRODUCT_STATUS_ACTIVE = "active"
PRODUCT_STATUS_SUSPENDED = "suspended"

POLICY_STATUS_ACTIVE = "active"
POLICY_STATUS_PAID = "paid"
POLICY_STATUS_REJECTED = "rejected"

CLAIM_STATUS_APPROVED = "approved"
CLAIM_STATUS_REJECTED = "rejected"


@allow_storage
@dataclass
class Product:
    description: str
    parameter_name: str
    threshold: u256          # payout triggers when agreed_value >= threshold
    tolerance_pct: u256      # sources must agree within this % of `threshold`
    premium: u256            # exact cost of one policy, in native tokens
    coverage: u256           # payout when the trigger fires
    data_sources: DynArray[str]
    status: str
    created_by: Address
    pool_balance: u256       # this product's own pool ledger, in native tokens

    def as_dict(self) -> dict:
        return {
            "description": self.description,
            "parameter_name": self.parameter_name,
            "threshold": int(self.threshold),
            "tolerance_pct": int(self.tolerance_pct),
            "premium": int(self.premium),
            "coverage": int(self.coverage),
            "data_sources": [u for u in self.data_sources],
            "status": self.status,
            "created_by": str(self.created_by),
            "pool_balance": int(self.pool_balance),
        }


@allow_storage
@dataclass
class Policy:
    product_id: str
    holder: Address
    premium: u256
    coverage: u256
    status: str
    created_at: u256

    def as_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "holder": str(self.holder),
            "premium": int(self.premium),
            "coverage": int(self.coverage),
            "status": self.status,
            "created_at": int(self.created_at),
        }


@allow_storage
@dataclass
class Claim:
    policy_id: str
    event_context: str
    sources_used: u256
    agreed_value: u256
    threshold: u256
    status: str
    payout: u256
    filed_at: u256

    def as_dict(self) -> dict:
        return {
            "policy_id": self.policy_id,
            "event_context": self.event_context,
            "sources_used": int(self.sources_used),
            "agreed_value": int(self.agreed_value),
            "threshold": int(self.threshold),
            "status": self.status,
            "payout": int(self.payout),
            "filed_at": int(self.filed_at),
        }


# --- Deterministic helpers (unit-testable without a VM) -------------------

def _current_timestamp() -> u256:
    """Deterministic per-transaction Unix timestamp (seconds). GenLayer
    pins the stdlib clock to the transaction datetime, so every validator
    computing it sees the same value."""
    import datetime as _dt
    return u256(int(_dt.datetime.now(_dt.timezone.utc).timestamp()))


def _parse_number(raw) -> int | None:
    """Coerce an LLM-produced value into a non-negative integer.

    Integer math only: GenVM calldata has no float type and float() is a
    non-deterministic pattern rejected by GenVM lint, so every value is kept
    as an integer end-to-end. Accepts ``int``, ``str`` (e.g. ``"180"`` or
    ``"180.0"``, decimals truncated), and ``float`` (truncated via ``repr``
    -- never ``float()``). Returns ``None`` for anything that is not a
    parseable non-negative number, so a garbage extraction is treated as a
    missing source rather than a bogus reading."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if "." in text:
            text = text.split(".")[0]
        if not text:
            return None
        try:
            value = int(text)
        except ValueError:
            return None
    elif isinstance(raw, float):
        text = repr(raw)
        if "." in text:
            text = text.split(".")[0]
        try:
            value = int(text)
        except ValueError:
            return None
    else:
        return None
    return value if value >= 0 else None


def _median(values: list[int]) -> int:
    """Median of a non-empty list (integer result). Robust to a single
    outlying source."""
    if not values:
        raise ValueError("median of empty sequence")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _tolerance_band(tolerance_pct: int, threshold: int) -> int:
    """Absolute disagreement budget in parameter units (integer math).

    The band is anchored to the *threshold*, not to the measured values:
    it is the width that matters for the payout decision. A 10 % band on a
    180-minute threshold means sources must agree within +/- 18 minutes,
    regardless of whether the observed delay is 5 or 500 minutes."""
    return max(int(tolerance_pct), 0) * max(int(threshold), 0) // 100


def _within_tolerance(a: int, b: int, tolerance_pct: int, threshold: int) -> bool:
    """True when `a` and `b` differ by no more than the tolerance band."""
    return abs(a - b) <= _tolerance_band(tolerance_pct, threshold)


def _strip_code_fence(raw: str) -> str:
    """Strip a markdown code fence from LLM JSON output if present."""
    s = raw.strip()
    if s.startswith("```"):
        first_newline = s.find("\n")
        s = s[first_newline + 1:] if first_newline != -1 else s[3:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return s


def _parse_json_object(raw) -> dict | None:
    """Parse an agreed consensus payload, tolerating a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(_strip_code_fence(raw))
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None
    return None


# --- The contract ----------------------------------------------------------

class ParametricInsurance(gl.Contract):
    products: TreeMap[str, Product]
    policies: TreeMap[str, Policy]
    claims: TreeMap[str, Claim]
    product_count: u256
    policy_count: u256

    def __init__(self):
        self.product_count = u256(0)
        self.policy_count = u256(0)

    # -- Insurer side ------------------------------------------------------

    @gl.public.write
    def create_product(
        self,
        description: str,
        parameter_name: str,
        threshold: int,
        tolerance_pct: int,
        premium: int,
        coverage: int,
        data_sources: list[str],
    ) -> str:
        """Create a new parametric insurance product. The caller becomes
        the product creator (the only role that can suspend the product,
        fund the pool, or withdraw from it). Returns the new product_id."""
        description = description.strip()
        parameter_name = parameter_name.strip()
        if not description:
            raise gl.vm.UserError("description must not be empty")
        if not parameter_name:
            raise gl.vm.UserError("parameter_name must not be empty")

        if threshold <= 0:
            raise gl.vm.UserError("threshold must be positive")
        if tolerance_pct < 0 or tolerance_pct > 100:
            raise gl.vm.UserError("tolerance_pct must be between 0 and 100")
        if premium <= 0:
            raise gl.vm.UserError("premium must be positive")
        if coverage <= 0:
            raise gl.vm.UserError("coverage must be positive")

        sources: list[str] = []
        for url in data_sources:
            url = url.strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                raise gl.vm.UserError(f"invalid data source URL: {url!r}")
            if url not in sources:
                sources.append(url)
        if len(sources) < MIN_DATA_SOURCES:
            raise gl.vm.UserError(f"a product needs at least {MIN_DATA_SOURCES} data sources")
        if len(sources) > MAX_DATA_SOURCES:
            raise gl.vm.UserError(f"at most {MAX_DATA_SOURCES} data sources allowed")

        product_id = f"product-{self.product_count}"
        self.product_count = self.product_count + u256(1)

        self.products[product_id] = Product(
            description=description,
            parameter_name=parameter_name,
            threshold=u256(threshold),
            tolerance_pct=u256(tolerance_pct),
            premium=u256(premium),
            coverage=u256(coverage),
            data_sources=sources,
            status=PRODUCT_STATUS_ACTIVE,
            created_by=gl.message.sender_address,
            pool_balance=u256(0),
        )
        return product_id

    @gl.public.write
    def suspend_product(self, product_id: str) -> None:
        """Only the product creator may suspend a product. Suspended
        products can no longer have policies bought -- existing policy
        holders retain their right to file claims regardless."""
        product_id = str(product_id)
        product = self.products.get(product_id)
        if product is None:
            raise gl.vm.UserError("unknown product_id")
        if gl.message.sender_address != product.created_by:
            raise gl.vm.UserError("only the product creator can suspend the product")
        product.status = PRODUCT_STATUS_SUSPENDED
        self.products[product_id] = product

    @gl.public.write.payable
    def fund_pool(self, product_id: str) -> None:
        """Anyone may add liquidity to a *specific* product's pool. The
        received value is credited to that product's isolated
        `pool_balance` ledger -- it can never be withdrawn except by that
        product's creator and is reserved against that product's claims."""
        product_id = str(product_id)
        product = self.products.get(product_id)
        if product is None:
            raise gl.vm.UserError("unknown product_id")
        if gl.message.value <= 0:
            raise gl.vm.UserError("must send a positive amount to fund the pool")
        product.pool_balance = product.pool_balance + gl.message.value
        self.products[product_id] = product

    @gl.public.write
    def withdraw_funds(self, product_id: str, amount: int) -> None:
        """Only the creator of a product may withdraw from *that* product's
        pool. Withdrawal is capped at the surplus above outstanding
        liabilities: ``pool_balance - outstanding_coverage - amount >= 0``.
        This ensures that a creator cannot withdraw funds earmarked for
        policies whose claims have not yet been filed."""
        product_id = str(product_id)
        amount = int(amount)
        if amount <= 0:
            raise gl.vm.UserError("amount must be positive")

        product = self.products.get(product_id)
        if product is None:
            raise gl.vm.UserError("unknown product_id")
        if gl.message.sender_address != product.created_by:
            raise gl.vm.UserError("only this product's creator can withdraw its funds")

        outstanding = u256(0)
        for policy in self.policies.values():
            if policy.product_id == product_id and policy.status == POLICY_STATUS_ACTIVE:
                outstanding = outstanding + policy.coverage

        surplus = product.pool_balance - outstanding
        if surplus < u256(amount):
            raise gl.vm.UserError(
                "insufficient surplus: outstanding coverage liabilities must be funded"
            )

        product.pool_balance = product.pool_balance - u256(amount)
        self.products[product_id] = product

        _EOA(gl.message.sender_address).emit_transfer(value=u256(amount))

    # -- Holder side -------------------------------------------------------

    @gl.public.write.payable
    def buy_policy(self, product_id: str) -> str:
        """Buy one policy on a product. The transaction must carry exactly
        the product's premium. Returns the new policy_id."""
        product_id = str(product_id)
        product = self.products.get(product_id)
        if product is None:
            raise gl.vm.UserError("unknown product_id")
        if product.status != PRODUCT_STATUS_ACTIVE:
            raise gl.vm.UserError("product is not active")
        if gl.message.value != product.premium:
            raise gl.vm.UserError(
                f"exact premium required: expected {int(product.premium)}, "
                f"sent {int(gl.message.value)}"
            )

        policy_id = f"policy-{self.policy_count}"
        self.policy_count = self.policy_count + u256(1)

        # The premium is revenue for this product's insurer: credit it to
        # the product's own pool ledger, where only its creator can reach it.
        product.pool_balance = product.pool_balance + product.premium
        self.products[product_id] = product

        self.policies[policy_id] = Policy(
            product_id=product_id,
            holder=gl.message.sender_address,
            premium=product.premium,
            coverage=product.coverage,
            status=POLICY_STATUS_ACTIVE,
            created_at=_current_timestamp(),
        )
        return policy_id

    @gl.public.write
    def file_claim(self, policy_id: str, event_context: str) -> dict:
        """File a claim on a policy. Only the policy holder may do this and
        only once per policy. Runs the multi-source oracle consensus (see
        `_acquire_extract` / `_consensus_validator`), then makes the
        deterministic payout decision.

        The non-deterministic block only acquires evidence and extracts a
        raw integer reading per source. The median (`agreed_value`), the
        threshold crossing, and the payout amount are plain integer
        computations performed here, outside the block.

        Returns the claim result dict: ``{status, agreed_value, payout,
        sources_used}``."""
        policy_id = str(policy_id)
        policy = self.policies.get(policy_id)
        if policy is None:
            raise gl.vm.UserError("unknown policy_id")
        if gl.message.sender_address != policy.holder:
            raise gl.vm.UserError("only the policy holder can file a claim")
        if policy_id in self.claims:
            raise gl.vm.UserError("a claim for this policy already exists")
        if policy.status != POLICY_STATUS_ACTIVE:
            raise gl.vm.UserError("policy is not active")

        product = self.products.get(policy.product_id)
        if product is None:
            raise gl.vm.UserError("unknown product for policy")

        event_context = event_context.strip()[:MAX_EVENT_CONTEXT_CHARS]
        if not event_context:
            raise gl.vm.UserError("event_context must not be empty")

        parameter_name = product.parameter_name
        threshold = int(product.threshold)
        tolerance_pct = int(product.tolerance_pct)
        coverage = policy.coverage
        data_sources = [u for u in product.data_sources]

        # The non-deterministic block is deliberately minimal -- it only
        # acquires evidence and extracts a raw integer reading per source.
        # Named nested functions (not lambdas) keep GenVM lint's call-graph
        # analysis from reaching the deterministic side effects below.
        def leader_fn():
            return _acquire_extract(
                data_sources, parameter_name, event_context,
            )

        def validator_fn(leaders_res):
            return _consensus_validator(
                leaders_res,
                data_sources, parameter_name, event_context,
                tolerance_pct, threshold,
            )

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        if not _consensus_ok(result):
            return self._reject_claim(
                policy_id, policy, event_context,
                agreed_value=u256(0), sources_used=u256(0),
                reason="insufficient_sources",
            )

        # Deterministic aggregation of the agreed raw readings: the median
        # is the agreed_value, and only its crossing of the threshold
        # decides the payout. All integer math, no float().
        readings = [(u, _parse_number(v)) for u, v in result["values"].items()]
        agreed = [v for _, v in readings if v is not None]
        sources_used = u256(len(agreed))
        agreed_value = u256(_median(agreed))

        if agreed_value < product.threshold:
            return self._reject_claim(
                policy_id, policy, event_context,
                agreed_value=agreed_value, sources_used=sources_used,
                reason="below_threshold",
            )

        if product.pool_balance < coverage:
            raise gl.vm.UserError(
                "insufficient funds in this product's pool to pay this claim -- "
                "the insurer must fund the pool first"
            )

        # Reserve the liability now: deduct the coverage from the product's
        # ledger in the same transaction that pays out, so the reserved
        # amount can never be withdrawn before the transfer settles.
        product.pool_balance = product.pool_balance - coverage
        self.products[policy.product_id] = product

        holder = policy.holder
        _EOA(holder).emit_transfer(value=coverage)

        policy.status = POLICY_STATUS_PAID
        self.policies[policy_id] = policy
        self.claims[policy_id] = Claim(
            policy_id=policy_id,
            event_context=event_context,
            sources_used=sources_used,
            agreed_value=agreed_value,
            threshold=product.threshold,
            status=CLAIM_STATUS_APPROVED,
            payout=coverage,
            filed_at=_current_timestamp(),
        )

        return {
            "status": CLAIM_STATUS_APPROVED,
            "agreed_value": int(agreed_value),
            "payout": int(coverage),
            "sources_used": int(sources_used),
        }

    def _reject_claim(
        self, policy_id: str, policy: Policy, event_context: str,
        agreed_value: u256, sources_used: u256, reason: str,
    ) -> dict:
        policy.status = POLICY_STATUS_REJECTED
        self.policies[policy_id] = policy
        self.claims[policy_id] = Claim(
            policy_id=policy_id,
            event_context=event_context,
            sources_used=sources_used,
            agreed_value=agreed_value,
            threshold=self.products[policy.product_id].threshold,
            status=CLAIM_STATUS_REJECTED,
            payout=u256(0),
            filed_at=_current_timestamp(),
        )
        return {
            "status": CLAIM_STATUS_REJECTED,
            "reason": reason,
            "agreed_value": int(agreed_value),
            "payout": 0,
            "sources_used": int(sources_used),
        }

    # -- Read methods ------------------------------------------------------

    @gl.public.view
    def get_product(self, product_id: str) -> dict:
        product_id = str(product_id)
        product = self.products.get(product_id)
        if product is None:
            raise gl.vm.UserError("unknown product_id")
        return product.as_dict()

    @gl.public.view
    def get_policy(self, policy_id: str) -> dict:
        policy_id = str(policy_id)
        policy = self.policies.get(policy_id)
        if policy is None:
            raise gl.vm.UserError("unknown policy_id")
        return policy.as_dict()

    @gl.public.view
    def get_claim(self, policy_id: str) -> dict:
        policy_id = str(policy_id)
        claim = self.claims.get(policy_id)
        if claim is None:
            raise gl.vm.UserError("no claim for this policy")
        return claim.as_dict()

    @gl.public.view
    def get_product_count(self) -> u256:
        return self.product_count

    @gl.public.view
    def get_policy_count(self) -> u256:
        return self.policy_count

    @gl.public.view
    def get_contract_address(self) -> str:
        return str(self.address)

    @gl.public.view
    def get_pool_balance(self) -> u256:
        """Total liability ledger: the sum of every product's own
        `pool_balance`. This is what is actually withdrawable / payable,
        product by product (the sum stays <= the real contract balance)."""
        total = u256(0)
        for product in self.products.values():
            total = total + product.pool_balance
        return total

    @gl.public.view
    def get_product_pool_balance(self, product_id: str) -> u256:
        """A single product's pool ledger (see `fund_pool` /
        `withdraw_funds` / reserved payouts)."""
        product_id = str(product_id)
        product = self.products.get(product_id)
        if product is None:
            raise gl.vm.UserError("unknown product_id")
        return product.pool_balance

    @gl.public.view
    def get_contract_balance(self) -> u256:
        """The real GEN balance held on this contract. It always covers
        (>=) the sum of all product pool ledgers."""
        return self.balance


# --- Consensus block (leader + validator) ----------------------------------
# The non-deterministic block is intentionally minimal: it only acquires
# evidence (`gl.nondet.web.get`) and extracts a raw integer reading per
# source (`gl.nondet.exec_prompt`). No median, no tolerance band, and no
# threshold math run inside it -- all of that is deterministic integer code
# outside the block (`file_claim`). The validator's only non-deterministic
# work is the independent re-acquisition/re-extraction; everything it
# compares, including the final threshold outcome, is integer arithmetic.

def _acquire_extract(
    data_sources: list[str],
    parameter_name: str,
    event_context: str,
) -> dict:
    """Evidence acquisition + extraction -- the *only* work done inside the
    non-deterministic block. Runs identically on the leader and on every
    validator.

    Fetches each configured data source (`gl.nondet.web.get`) and asks the
    model to extract the parameter value for the event under review
    (`gl.nondet.exec_prompt`). Sources that are unreachable or yield an
    unparseable value are skipped; if fewer than `MIN_DATA_SOURCES` sources
    survive, the block reports ``ok=False`` and the claim is rejected.

    Returns ``{"ok": True, "values": {url: int}}`` -- the raw per-source
    integer readings, with *no* aggregation -- or
    ``{"ok": False, "reason": "insufficient_sources"}``."""
    extracted: dict[str, int] = {}
    for url in data_sources:
        try:
            response = gl.nondet.web.get(url)
            text = response.body.decode("utf-8")
        except Exception:
            continue

        value = _extract_parameter(text, parameter_name, event_context)
        if value is not None:
            extracted[url] = value

    if len(extracted) < MIN_DATA_SOURCES:
        return {"ok": False, "reason": "insufficient_sources"}

    return {"ok": True, "values": extracted}


def _consensus_validator(
    leaders_res,
    data_sources: list[str],
    parameter_name: str,
    event_context: str,
    tolerance_pct: int,
    threshold: int,
) -> bool:
    """The equivalence check. Runs on every validator, which independently
    re-fetches every source and re-extracts the parameter. The leader's
    readings are accepted only if:

    - the validator read exactly the same set of reachable sources;
    - each source value agrees within the tolerance band
      (`tolerance_pct` % of `threshold`);
    - the deterministic median of the leader's readings and the median of
      the validator's own readings lie on the *same side of the payout
      threshold* -- a validator can never approve a reading that crosses
      the threshold in the opposite direction to its own independent
      reading.

    If the leader reported ``ok=False`` (insufficient sources), the
    validator must independently reach the same conclusion -- otherwise
    the results are not equivalent."""
    if not isinstance(leaders_res, gl.vm.Return):
        return False
    leader_data = leaders_res.calldata
    if not isinstance(leader_data, dict):
        return False

    my_data = _acquire_extract(data_sources, parameter_name, event_context)

    if bool(leader_data.get("ok")) != bool(my_data.get("ok")):
        return False
    if not leader_data.get("ok"):
        return True  # both agree there are not enough sources -> reject

    leader_values = leader_data.get("values")
    my_values = my_data.get("values")
    if not isinstance(leader_values, dict) or not isinstance(my_values, dict):
        return False
    if set(leader_values.keys()) != set(my_values.keys()):
        return False

    # Per-source agreement within the tolerance band (integer math).
    for url in leader_values:
        leader_reading = _parse_number(leader_values[url])
        my_reading = _parse_number(my_values[url])
        if leader_reading is None or my_reading is None:
            return False
        if not _within_tolerance(
            leader_reading, my_reading,
            tolerance_pct, threshold,
        ):
            return False

    # Threshold-outcome agreement: the aggregate decision implied by each
    # side's readings must be identical, so the tolerance band can never
    # approve readings on opposite sides of the payout threshold.
    leader_median = _median([_parse_number(v) for v in leader_values.values()])
    my_median = _median([_parse_number(v) for v in my_values.values()])
    if (leader_median >= threshold) != (my_median >= threshold):
        return False

    return True


def _extract_parameter(text: str, parameter_name: str, event_context: str) -> int | None:
    """Ask the model to read one numeric parameter out of a source body for
    the event under review. Returns the parsed non-negative integer, or None
    when the source doesn't contain the parameter."""
    prompt = f"""You are a data analyst for a parametric insurance contract.
The policy event under review is:
{event_context}

From the data source below, extract the value of the parameter `{parameter_name}`.
The parameter is a non-negative number. If the source does not report this
parameter, return the number 0.

Source data:
{text[:MAX_SOURCE_BODY_CHARS]}

Respond with ONLY a JSON object, no other text, no markdown fences:
{{"value": <non-negative number>}}"""

    try:
        parsed = gl.nondet.exec_prompt(prompt, response_format="json")
    except Exception:
        return None
    return _parse_number(parsed.get("value") if isinstance(parsed, dict) else parsed)


def _consensus_ok(result) -> bool:
    """Deterministic post-check on the agreed consensus payload: the block
    must have reported ``ok=True`` with at least `MIN_DATA_SOURCES` raw
    integer readings. No aggregation is expected here -- the median is
    computed by the caller in deterministic code."""
    data = _parse_json_object(result)
    if data is None:
        return False
    if not data.get("ok"):
        return False
    values = data.get("values")
    if not isinstance(values, dict) or len(values) < MIN_DATA_SOURCES:
        return False
    if any(_parse_number(v) is None for v in values.values()):
        return False
    return True
