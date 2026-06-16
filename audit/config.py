"""Tunable policy thresholds for the audit engine.

Keep ALL business numbers here so judges can see (and you can tweak live) the
rules without touching engine code.
"""

# GST on gold jewellery in India is 3%. Tax should reconcile to this.
GST_RATE = 0.03

# Money comparisons tolerate small rounding noise (the real data rounds amounts).
MONEY_TOLERANCE = 1.0  # rupees

# Maximum total discount % allowed per order channel/type.
# A discount above the cap (without an authorisation flag) is revenue leakage.
DISCOUNT_CAP = {
    "EZ":      20.0,   # EZ / exchange orders
    "JM":      15.0,   # store (jewellery) orders
    "JR":      15.0,   # repair orders
    "ONLINE":  15.0,   # online / web orders
    "OLDGOLD": 25.0,   # old-gold exchange can carry higher effective discount
    "DEFAULT": 15.0,
}

# Orders at or above this grand total must carry financial_approval == YES.
FINANCIAL_APPROVAL_THRESHOLD = 100000.0  # rupees

# Item statuses that mean the item is being / has been shipped -> must have an invoice.
SHIPPABLE_STATUSES = {"Dispatched", "Shipped", "Complete", "Invoiced", "Delivered"}

# Severity ranking (higher = worse). Used for sorting + verdict.
SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}

# An order is FLAGGED if it has any finding at or above this severity.
FLAG_AT = "HIGH"
