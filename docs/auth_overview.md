# Authentication Overview

The auth module covers account registration, login, and session revocation.
Passwords are stored as salted SHA-256 hashes and are never returned by any
function. Session tokens are opaque strings valid for 12 hours.

Login failures intentionally return the same error for unknown emails and
wrong passwords, so the API cannot be used to probe which emails are
registered.
