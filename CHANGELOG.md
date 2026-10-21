# Changelog

## 0.2.0 — 2026-10-20

### Added

- Requests made through the provider now identify it: `User-Agent: featureflip-python/<version> featureflip-openfeature-python/<version>`, so Featureflip can count OpenFeature usage apart from direct SDK use. This applies when the provider builds its own client from an SDK key. A client you pass in was built before the provider saw it, so its requests are unchanged; the same is true if your `Config` already names a wrapper, which is kept. Requires `featureflip` 2.8.0 or later, which this release now depends on. (#4153)

## 0.1.1 — 2026-08-29

### Fixed

- The README and docs quickstart called `api.set_provider(...)`, which runs the provider's `initialize()` on a background thread and returns immediately. Anyone following it verbatim raced the initial flag load, so the evaluations on the following lines could return their defaults instead of real values. Both now use `api.set_provider_and_wait(...)`, the Python equivalent of awaiting `setProviderAndWait` in Node, and the reason is stated inline so the racy form is not reintroduced. No provider code changed — only the guidance was wrong. (#2586)

  The end-to-end tests now register through that documented path rather than pre-initializing the provider by hand and then calling `set_provider`, which is why the whole 0.1.0 cycle could recommend the racy call with a green suite. The new guard asserts on `error_code` rather than only the value: against fast test doubles the init thread usually wins the race, so a value-only assertion passes most runs and would be flaky rather than protective, while an uninitialized provider deterministically reports `PROVIDER_NOT_READY`.

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
