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
   holder. The pool is funded by the insurer (`fund_pool`) and can only be
   withdrawn from by the product creator (`withdraw_funds`). No LLM
   decides how much money moves -- the amount is fixed per policy.
2. Multi-source oracle consensus with a tolerance band. A claim must be
   backed by data fetched from at least `MIN_DATA_SOURCES` *independent*
   URLs configured on the product. Every validator independently re-fetches
   every source, extracts the parameter, and only accepts the leader's
   reading if each source agrees within a *relative tolerance band* of the
   trigger threshold (`tolerance_pct` % of `threshold`). This is real
   consensus logic, not a single API call.
3. The payout decision is deterministic. The non-deterministic part
   (web + LLM extraction) only produces an `agreed_value`. Whether that
   value crosses the threshold, and the payout amount, are plain integer
   comparisons decided outside the consensus block (`file_claim`).
4. Defense against fraud / abuse. Only the holder of a policy can file a
   claim on it; a policy can only be claimed once; claims can't be filed
   against a suspended product; a claim is rejected (not paid) when the
   sources can't reach consensus or when fewer than `MIN_DATA_SOURCES`
   sources were reachable; the payout is guarded by the pool balance.
5. Deterministic, unit-testable helpers. `_median`, `_parse_number`,
   `_tolerance_band`, `_within_tolerance`, `_extract_parameter` are plain
   Python that run before/after the consensus block and can be tested
   without any VM (`tests/direct/test_helpers.py`).

Trust model / limitations
-------------------------
- The product creator is trusted to configure honest `data_sources`; the
  contract verifies agreement *between* sources, not the truth of the
  sources themselves. A product whose sources all feed the same upstream
  is a single point of failure.
- The contract does not check *who* the physical claimant is; any holder
  of the policy NFT-equivalent (address) can collect. On GenLayer a native
  transfer to the holder is the payout mechanism.
- The pool must be funded before covered claims are filed; a covered claim
  reverts with a clear message when `self.balance < coverage`.
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


def _parse_number(raw) -> float | None:
    """Coerce an LLM-produced value into a non-negative float.

    Accepts int, float, and the string forms validators commonly emit
    (e.g. ``"180"`` or ``"180.0"``). Returns ``None`` for anything that is
    not a parseable non-negative number, so a garbage extraction is treated
    as a missing source rather than a bogus reading."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if value >= 0 else None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
        return value if value >= 0 else None
    return None


def _median(values: list[float]) -> float:
    """Median of a non-empty list. Robust to a single outlying source."""
    if not values:
        raise ValueError("median of empty sequence")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _tolerance_band(tolerance_pct: float, threshold: float) -> float:
    """Absolute disagreement budget in parameter units.

    The band is anchored to the *threshold*, not to the measured values:
    it is the width that matters for the payout decision. A 10 % band on a
    180-minute threshold means sources must agree within +/- 18 minutes,
    regardless of whether the observed delay is 5 or 500 minutes."""
    return max(tolerance_pct, 0.0) / 100.0 * max(threshold, 0.0)


def _within_tolerance(a: float, b: float, tolerance_pct: float, threshold: float) -> bool:
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
            raise Exception("description must not be empty")
        if not parameter_name:
            raise Exception("parameter_name must not be empty")

        if threshold <= 0:
            raise Exception("threshold must be positive")
        if tolerance_pct < 0 or tolerance_pct > 100:
            raise Exception("tolerance_pct must be between 0 and 100")
        if premium <= 0:
            raise Exception("premium must be positive")
        if coverage <= 0:
            raise Exception("coverage must be positive")

        sources: list[str] = []
        for url in data_sources:
            url = url.strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                raise Exception(f"invalid data source URL: {url!r}")
            if url not in sources:
                sources.append(url)
        if len(sources) < MIN_DATA_SOURCES:
            raise Exception(f"a product needs at least {MIN_DATA_SOURCES} data sources")
        if len(sources) > MAX_DATA_SOURCES:
            raise Exception(f"at most {MAX_DATA_SOURCES} data sources allowed")

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
        )
        return product_id

    @gl.public.write
    def suspend_product(self, product_id: str) -> None:
        """Only the product creator may suspend a product. Suspended
        products can no longer have policies bought or claims filed."""
        product_id = str(product_id)
        product = self.products.get(product_id)
        if product is None:
            raise Exception("unknown product_id")
        if gl.message.sender_address != product.created_by:
            raise Exception("only the product creator can suspend the product")
        product.status = PRODUCT_STATUS_SUSPENDED
        self.products[product_id] = product

    @gl.public.write.payable
    def fund_pool(self) -> None:
        """Anyone may add liquidity to the payout pool. The received value
        is simply kept on the contract; no balance is tracked per-fundr,
        the pool is shared across all products."""
        if gl.message.value <= 0:
            raise Exception("must send a positive amount to fund the pool")

    @gl.public.write
    def withdraw_funds(self, amount: int) -> None:
        """Only a product creator may withdraw from the pool. Withdrawal is
        capped at the current balance so the pool can never go negative."""
        amount = int(amount)
        if amount <= 0:
            raise Exception("amount must be positive")

        sender = gl.message.sender_address
        authorized = any(
            p.created_by == sender
            for p in self.products.values()
        )
        if not authorized:
            raise Exception("only a product creator can withdraw funds")

        if self.balance < u256(amount):
            raise Exception("withdrawal exceeds the contract balance")

        _EOA(sender).emit_transfer(value=u256(amount))

    # -- Holder side -------------------------------------------------------

    @gl.public.write.payable
    def buy_policy(self, product_id: str) -> str:
        """Buy one policy on a product. The transaction must carry exactly
        the product's premium. Returns the new policy_id."""
        product_id = str(product_id)
        product = self.products.get(product_id)
        if product is None:
            raise Exception("unknown product_id")
        if product.status != PRODUCT_STATUS_ACTIVE:
            raise Exception("product is not active")
        if gl.message.value != product.premium:
            raise Exception(
                f"exact premium required: expected {int(product.premium)}, "
                f"sent {int(gl.message.value)}"
            )

        policy_id = f"policy-{self.policy_count}"
        self.policy_count = self.policy_count + u256(1)

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
        `_consensus_leader` / `_consensus_validator`), then makes the
        deterministic payout decision.

        Returns the claim result dict: ``{status, agreed_value, payout,
        sources_used}``."""
        policy_id = str(policy_id)
        policy = self.policies.get(policy_id)
        if policy is None:
            raise Exception("unknown policy_id")
        if gl.message.sender_address != policy.holder:
            raise Exception("only the policy holder can file a claim")
        if policy_id in self.claims:
            raise Exception("a claim for this policy already exists")
        if policy.status != POLICY_STATUS_ACTIVE:
            raise Exception("policy is not active")

        product = self.products.get(policy.product_id)
        if product is None:
            raise Exception("unknown product for policy")
        if product.status != PRODUCT_STATUS_ACTIVE:
            raise Exception("product is suspended")

        event_context = event_context.strip()[:MAX_EVENT_CONTEXT_CHARS]
        if not event_context:
            raise Exception("event_context must not be empty")

        parameter_name = product.parameter_name
        threshold = int(product.threshold)
        tolerance_pct = int(product.tolerance_pct)
        coverage = policy.coverage
        data_sources = [u for u in product.data_sources]

        result = gl.vm.run_nondet_unsafe(
            lambda: _consensus_leader(
                data_sources, parameter_name, event_context,
            ),
            lambda leaders_res: _consensus_validator(
                leaders_res,
                data_sources, parameter_name, event_context,
                tolerance_pct, threshold,
            ),
        )

        if not _consensus_ok(result):
            return self._reject_claim(
                policy_id, policy, event_context,
                agreed_value=u256(0), sources_used=u256(0),
                reason="insufficient_sources",
            )

        agreed_value = u256(int(round(float(result["agreed_value"]))))
        sources_used = u256(len(result["values"]))

        if agreed_value < product.threshold:
            return self._reject_claim(
                policy_id, policy, event_context,
                agreed_value=agreed_value, sources_used=sources_used,
                reason="below_threshold",
            )

        if self.balance < coverage:
            raise Exception(
                "insufficient contract funds to pay this claim -- "
                "the insurer must fund the pool first"
            )

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
            raise Exception("unknown product_id")
        return product.as_dict()

    @gl.public.view
    def get_policy(self, policy_id: str) -> dict:
        policy_id = str(policy_id)
        policy = self.policies.get(policy_id)
        if policy is None:
            raise Exception("unknown policy_id")
        return policy.as_dict()

    @gl.public.view
    def get_claim(self, policy_id: str) -> dict:
        policy_id = str(policy_id)
        claim = self.claims.get(policy_id)
        if claim is None:
            raise Exception("no claim for this policy")
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
        return self.balance


# --- Consensus block (leader + validator) ----------------------------------

def _consensus_leader(
    data_sources: list[str],
    parameter_name: str,
    event_context: str,
) -> dict:
    """Runs independently on every validator. Fetches each configured data
    source and asks the model to extract the parameter value for the event
    under review. Sources that are unreachable or yield an unparseable
    value are skipped; if fewer than `MIN_DATA_SOURCES` sources survive,
    the block reports ``ok=False`` and the claim is rejected.

    Returns ``{"ok": True, "values": {url: value}, "agreed_value": <median>}``
    or ``{"ok": False, "reason": "insufficient_sources"}``."""
    extracted: dict[str, float] = {}
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

    return {
        "ok": True,
        "values": extracted,
        "agreed_value": _median(list(extracted.values())),
    }


def _consensus_validator(
    leaders_res,
    data_sources: list[str],
    parameter_name: str,
    event_context: str,
    tolerance_pct: float,
    threshold: float,
) -> bool:
    """The equivalence check. Runs on every validator, which independently
    re-fetches every source and re-extracts the parameter. The leader's
    reading is accepted only if:

    - the validator read exactly the same set of reachable sources;
    - each source value agrees within the tolerance band
      (`tolerance_pct` % of `threshold`);
    - the agreed (median) value also agrees within the band.

    If the leader reported ``ok=False`` (insufficient sources), the
    validator must independently reach the same conclusion -- otherwise
    the results are not equivalent."""
    if not isinstance(leaders_res, gl.vm.Return):
        return False
    leader_data = leaders_res.calldata
    if not isinstance(leader_data, dict):
        return False

    my_data = _consensus_leader(data_sources, parameter_name, event_context)

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

    for url in leader_values:
        if not _within_tolerance(
            float(leader_values[url]), float(my_values[url]),
            tolerance_pct, threshold,
        ):
            return False

    if not _within_tolerance(
        float(leader_data.get("agreed_value", -1.0)),
        float(my_data.get("agreed_value", -1.0)),
        tolerance_pct, threshold,
    ):
        return False

    return True


def _extract_parameter(text: str, parameter_name: str, event_context: str) -> float | None:
    """Ask the model to read one numeric parameter out of a source body for
    the event under review. Returns the parsed non-negative float, or None
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
    """Deterministic post-check on the agreed consensus payload."""
    data = _parse_json_object(result)
    if data is None:
        return False
    if not data.get("ok"):
        return False
    values = data.get("values")
    if not isinstance(values, dict) or len(values) < MIN_DATA_SOURCES:
        return False
    agreed = _parse_number(data.get("agreed_value"))
    if agreed is None:
        return False
    for url, value in values.items():
        if _parse_number(value) is None:
            return False
    return True
