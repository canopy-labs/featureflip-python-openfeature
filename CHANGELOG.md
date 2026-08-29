# Changelog

## 0.1.0 — 2026-08-28

Initial release of the OpenFeature provider for the Featureflip Python server SDK.

### Added

- `FeatureflipProvider`, backed by the Featureflip Python SDK. Construct it with an SDK key (the provider creates and owns the client) or with an existing `FeatureflipClient` (the caller keeps ownership, and the provider will not close it). The SDK key form defers client construction to `initialize()`, because `FeatureflipClient.__init__` blocks on the initial flag fetch — building it eagerly would move that wait out of `OpenFeature.set_provider_and_wait()` and into your constructor. (#2586)

- Context mapping: `targeting_key` becomes `user_id`, the canonical wire field that percentage rollouts bucket on, unless the caller already supplied `user_id` or its accepted alias `userId`. All other attributes pass through unchanged.

- Reason mapping: `RULE_MATCH` → `TARGETING_MATCH`, `FALLTHROUGH` → `DEFAULT`, `FLAG_DISABLED` → `DISABLED`. An unmet prerequisite surfaces as the custom reason `PREREQUISITE_FAILED` — no standard OpenFeature reason models one, and reasons are open strings, so mislabelling it would be worse than naming it. `ruleId` and `prerequisiteKey` are carried in `flag_metadata`.

- On an error reason (`FLAG_NOT_FOUND`/`ERROR`) the caller's default is returned rather than `detail.value`. The SDK's error details can carry a flag's off-variation value — a prerequisite chain past the depth limit, or an errored prerequisite — which would be wrong-typed, and the caller's default is what the OpenFeature error contract prescribes.

- A runtime type guard enforcing OpenFeature's `TYPE_MISMATCH` contract, which Featureflip does not enforce itself. `get_integer_value` accepts whole-number floats (JSON does not distinguish `1` from `1.0`), `get_float_value` accepts any number, and `get_object_value` accepts objects and arrays but not strings. Booleans are rejected by both numeric accessors: `bool` is a subclass of `int` in Python, so without an explicit exclusion a boolean flag would satisfy `get_integer_value` and return `1`.

- `PROVIDER_CONFIGURATION_CHANGED` is emitted whenever flag configuration changes, with `flags_changed` naming the affected keys. The initial load does not raise it — OpenFeature signals that with `PROVIDER_READY`. Requires `featureflip >= 2.7.0`, which added the underlying update hook.

- `track()` forwards to the SDK's custom event tracking, mapping `TrackingEventDetails.attributes` to the event metadata and including a numeric `value` under the `value` key.
