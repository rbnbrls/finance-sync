# Subscription Detection

The subscription detection system identifies recurring transactions in a user's
transaction history using a two-pronged approach:

1. **Pattern recognition** — analyses amounts, intervals, and merchant identity
   to detect recurring patterns.
2. **Merchant classification** — uses a known-merchant ticker map and GICS
   sector data to classify merchants as likely subscription providers.

When both methods independently detect the same merchant, their results are
cross-validated for higher confidence.

## Architecture

```
src/finance_sync/services/subscription_detector/
├── __init__.py            # Package exports, re-exports all public types
├── detector.py            # Original SubscriptionDetector (DB-backed service)
├── merchant_classifier.py # Standalone merchant classification (GICS sector)
├── pattern_detector.py    # Standalone pattern recognition (amount + interval)
└── service.py             # Integrated SubscriptionDetectionService
```

## Components

### PatternDetector (`pattern_detector.py`)

Pure-function pattern recognition operating on transaction dicts. No database
dependency.

- Groups transactions by normalised merchant name
- Checks amount consistency (relative ≤5% or absolute ≤€2.00)
- Detects interval regularity (weekly, biweekly, monthly, quarterly, etc.)
- Computes confidence score from occurrences, consistency, regularity, keywords
- Accepts optional merchant classifications for sector enrichment

```python
detector = PatternDetector(min_occurrences=2)
results: list[PatternResult] = detector.detect(transactions)
```

### Merchant Classifier (`merchant_classifier.py`)

Classifies a merchant's subscription likelihood based on:

1. **Merchant → ticker map** — known subscription merchants resolve to GICS
   sector and ticker (e.g. Netflix → Communication Services / NFLX).
2. **Category-based sector mapping** — unknown merchants with known categories
   (e.g. "streaming", "software") map to a GICS sector.
3. **Sector-based default** — falls back to medium likelihood.

```python
result = classify_merchant("Netflix B.V.")
# MerchantClass(is_subscription=True, sector="Communication Services", ticker="NFLX")
```

### SubscriptionDetectionService (`service.py`)

The integrated service that combines both methods:

1. **Classify all merchants** in the transaction set.
2. **Run pattern detection** grouped by (merchant, amount bucket).
3. **Cross-validate** — when both methods detect the same merchant, apply
   a +0.10 bonus and upgrade to `HYBRID` detection method.
4. **Merchant-only fallback** — classified merchants with <2 occurrences are
   still included (useful for new subscriptions).
5. **Cancellation signals** — transactions containing cancellation keywords
   (cancel, refund, etc.) mark subscriptions as `CANCELLED`.

```python
svc = SubscriptionDetectionService()
subs = await svc.detect_subscriptions(
    user_id="test",
    transactions=my_txns,
)
```

### Edge Cases Handled

| Edge case | Approach |
|-----------|----------|
| Amount variations (±5% or ±€2) | Tolerated via relative + absolute thresholds |
| Irregular intervals (CV > 0.5) | Confidence degraded but not discarded |
| Skipped occurrences | Gap tolerated if surrounding intervals match |
| Zero amounts | Configurable `allow_zero_amount` parameter |
| Multiple plans from same merchant | Grouped by (merchant, amount bucket) |
| Same merchant, different currency | Separated by currency code |
| Cancellation signals | Detected via regex in transaction descriptions |
| Overlapping subscriptions | Resolved via 2% amount tolerance dedup |
| Single occurrence of known merchant | Included via merchant-classification-only path |

## Confidence Scoring

Scores are 0.0–1.0 composed from:

- **Occurrence count** (max 0.30): ≥12 occurrences = 0.30
- **Amount consistency** (max 0.25): exact = 0.25, degraded for variance
- **Interval regularity** (max 0.25): CV ≤ 0.1 = 0.25
- **Keyword bonus** (+0.12): subscription-related keywords in description
- **Category bonus** (+0.08): categorised merchant
- **Sector boost** (+0.00–0.12): from merchant classification likelihood
- **Cross-validation bonus** (+0.10): both methods agree

**Thresholds:** ≥0.80 = HIGH, ≥0.50 = MEDIUM, <0.50 = LOW

## Detection Methods

| Method | Description |
|--------|-------------|
| `EXACT_AMOUNT` | Exact same amount, regular interval |
| `SIMILAR_AMOUNT` | Similar amount (±5%), regular interval |
| `REGULAR_INTERVAL` | Regular timing, any amount |
| `MERCHANT_CLASSIFICATION` | Identified via sector/ticker lookup |
| `HYBRID` | Both pattern + merchant classification agree (highest confidence) |

## API Endpoints

See `docs/API.md` for full API reference. Key subscription endpoints:

||| Method | Path | Description |
||--------|------|-------------|
|| GET | `/subscriptions/detected` | Run integrated detection (read-only, ephemeral) |
|| POST | `/subscriptions/detect` | Run integrated detection (ephemeral) with request body |
|| POST | `/subscriptions/analyze` | Run integrated detection (ephemeral, backward-compatible) |
|| GET | `/subscriptions` | List persisted subscriptions |
|| GET | `/subscriptions/{id}` | Get single subscription |
|| PATCH | `/subscriptions/{id}` | Update status/category/notes |
|| POST | `/subscriptions/{id}/confirm` | Confirm a subscription |
|| POST | `/subscriptions/{id}/ignore` | Ignore a subscription |
|| DELETE | `/subscriptions/{id}` | Delete a subscription |

## Tests

- `tests/test_subscription_detection_service.py` — integrated service tests
- `tests/test_subscription_detection_extras.py` — additional service coverage
- `tests/test_subscription_detection_integration.py` — end-to-end integration
- `tests/test_pattern_detector_standalone.py` — PatternDetector unit tests
- `tests/test_merchant_classifier_standalone.py` — MerchantClassifier unit tests
- `tests/test_detection_algorithms_edge_cases.py` — edge case and boundary tests
- `tests/test_subscriptions_api.py` — API endpoint tests
- `tests/test_pattern_clustering.py` — clustering-based detection tests
