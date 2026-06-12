"""Payment processing module for the sample storefront."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

_TRANSACTIONS: dict[str, dict] = {}

SUPPORTED_CURRENCIES = {"USD", "EUR", "GBP"}


def process_payment(
    amount: Decimal,
    currency: str,
    customer_id: str,
    payment_method: str = "card",
    idempotency_key: str | None = None,
) -> dict:
    """Charge a customer and record the transaction.

    Args:
        amount: Charge amount, must be positive.
        currency: ISO 4217 currency code; one of USD, EUR, GBP.
        customer_id: Identifier of the paying customer.
        payment_method: How the customer pays; defaults to "card".
        idempotency_key: Optional key to make retries safe. If a transaction
            with this key already exists, it is returned instead of charging
            again.

    Returns:
        The transaction record with keys: transaction_id, amount, currency,
        customer_id, payment_method, status, created_at.

    Raises:
        ValueError: If amount is not positive or currency is unsupported.
    """
    if amount <= 0:
        raise ValueError("amount must be positive")
    if currency not in SUPPORTED_CURRENCIES:
        raise ValueError(f"unsupported currency: {currency}")

    if idempotency_key:
        for txn in _TRANSACTIONS.values():
            if txn.get("idempotency_key") == idempotency_key:
                return txn

    transaction = {
        "transaction_id": str(uuid4()),
        "amount": str(amount),
        "currency": currency,
        "customer_id": customer_id,
        "payment_method": payment_method,
        "status": "captured",
        "idempotency_key": idempotency_key,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _TRANSACTIONS[transaction["transaction_id"]] = transaction
    return transaction


def refund_transaction(transaction_id: str, amount: Decimal | None = None) -> dict:
    """Refund a captured transaction, fully or partially.

    Args:
        transaction_id: The transaction to refund.
        amount: Amount to refund. If None, the full transaction amount is
            refunded.

    Returns:
        The updated transaction record with status "refunded" or
        "partially_refunded" and a refunded_amount field.

    Raises:
        KeyError: If the transaction does not exist.
        ValueError: If the transaction is not in "captured" status, or the
            refund amount exceeds the original charge.
    """
    transaction = _TRANSACTIONS[transaction_id]
    if transaction["status"] != "captured":
        raise ValueError(f"cannot refund transaction in status {transaction['status']}")

    original = Decimal(transaction["amount"])
    refund_amount = amount if amount is not None else original
    if refund_amount > original:
        raise ValueError("refund amount exceeds original charge")

    transaction["refunded_amount"] = str(refund_amount)
    transaction["status"] = "refunded" if refund_amount == original else "partially_refunded"
    return transaction


def get_transaction_history(customer_id: str, limit: int = 50) -> list[dict]:
    """List a customer's transactions, newest first.

    Args:
        customer_id: The customer whose history to fetch.
        limit: Maximum number of transactions to return; defaults to 50.

    Returns:
        A list of transaction records sorted by created_at descending. Empty
        list if the customer has no transactions.
    """
    history = [t for t in _TRANSACTIONS.values() if t["customer_id"] == customer_id]
    history.sort(key=lambda t: t["created_at"], reverse=True)
    return history[:limit]
