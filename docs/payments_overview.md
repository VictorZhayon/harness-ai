# Payments Overview

The payments module handles charging customers, refunds, and transaction
history for the storefront. All amounts use `Decimal` and the module supports
USD, EUR and GBP.

Charges are created with an optional idempotency key so client retries never
double-charge a customer. Refunds can be full or partial; a partial refund
moves the transaction to a `partially_refunded` status.

See also the authentication docs for how customer sessions are established
before payment endpoints can be called.
