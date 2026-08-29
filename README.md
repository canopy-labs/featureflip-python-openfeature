# Featureflip OpenFeature Provider (Python)

[OpenFeature](https://openfeature.dev/) provider for
[Featureflip](https://featureflip.io), backed by the Featureflip Python server
SDK. Use the vendor-neutral OpenFeature API while Featureflip serves your flags.

## Installation

```bash
pip install featureflip-openfeature-provider
```

`featureflip` and `openfeature-sdk` are declared as lower-bounded dependencies
rather than pinned — your application controls their versions, and a single
`featureflip` copy is what lets this provider and any direct SDK usage share one
underlying client core.

## Quickstart

```python
from openfeature import api
from featureflip_openfeature import FeatureflipProvider

api.set_provider_and_wait(FeatureflipProvider(sdk_key="your-server-sdk-key"))

client = api.get_client()

enabled = client.get_boolean_value(
    "new-checkout",
    False,
    EvaluationContext(targeting_key="user-42", attributes={"plan": "pro"}),
)
```

The provider owns the client it creates and closes it on shutdown. To share one
client with code that uses the SDK directly, construct the client yourself and
pass it in — the provider will not close a client it did not create:

```python
from featureflip import FeatureflipClient

client = FeatureflipClient(sdk_key="your-server-sdk-key")
api.set_provider_and_wait(FeatureflipProvider(client=client))
```

Note that `FeatureflipClient(...)` blocks on the initial flag fetch. With the
`sdk_key` form the provider defers that to `initialize()`, so the wait happens
inside OpenFeature's provider setup rather than in your constructor.

Use `set_provider_and_wait`, not `set_provider`. The plain form runs the
provider's `initialize()` on a background thread and returns immediately, so
flags may not have loaded yet and the evaluations right after it can return
your defaults. `set_provider_and_wait` blocks until initialization finishes.

## Context mapping

| OpenFeature context | Featureflip context |
|---|---|
| `targeting_key` | `user_id` (used for rollout bucketing) |
| Any other attribute | Passed through unchanged |

An explicit `user_id` (or its alias `userId`) attribute takes precedence over
`targeting_key`. Without either, percentage rollouts bucketed by user serve the
control variation — see the SDK's keyless-context behavior.

## Evaluation reasons

| Featureflip reason | OpenFeature reason |
|---|---|
| `RULE_MATCH` | `TARGETING_MATCH` |
| `FALLTHROUGH` | `DEFAULT` |
| `FLAG_DISABLED` | `DISABLED` |
| `PREREQUISITE_FAILED` | `PREREQUISITE_FAILED` (custom) |
| `FLAG_NOT_FOUND` | `ERROR` + `FLAG_NOT_FOUND` |
| `ERROR` | `ERROR` + `GENERAL` |

No standard OpenFeature reason models an unmet prerequisite. Reasons are open
strings, so the provider surfaces `PREREQUISITE_FAILED` verbatim rather than
mislabelling it.

On an error reason the provider returns **your default value**, not the flag's
off-variation — that value can be wrong-typed, and the caller's default is what
the OpenFeature error contract prescribes.

### Flag metadata

Successful resolutions carry `ruleId` and `prerequisiteKey` in `flag_metadata`
when the evaluation produced them.

## Type handling

Featureflip does not type-check flag values against the accessor you call, so
the provider enforces OpenFeature's `TYPE_MISMATCH` contract itself. A
mismatched read returns your default with reason `ERROR` and error code
`TYPE_MISMATCH`.

| Accessor | Accepts |
|---|---|
| `get_boolean_value` | booleans only |
| `get_string_value` | strings only |
| `get_integer_value` | integers, and whole-number floats (`1.0`, `1e2`) |
| `get_float_value` | any JSON number, including integers |
| `get_object_value` | objects and arrays only — not strings |

Booleans are deliberately rejected by the numeric accessors. `bool` is a
subclass of `int` in Python, so without an explicit exclusion a boolean flag
would satisfy `get_integer_value` and hand you `1`.

## Events

The provider emits OpenFeature's `PROVIDER_CONFIGURATION_CHANGED` whenever
Featureflip flag configuration changes, with `flags_changed` naming the affected
keys:

```python
from openfeature.event import ProviderEvent

client.add_handler(
    ProviderEvent.PROVIDER_CONFIGURATION_CHANGED,
    lambda details: print("changed:", details.flags_changed),
)
```

The initial flag load does not raise this event — OpenFeature signals that with
`PROVIDER_READY`. Beyond flags whose own configuration changed, the event also
names flags whose evaluated value moves as a result: those referencing an edited
segment, and those depending on a changed flag through a prerequisite.

Requires `featureflip >= 2.7.0`, which added the underlying update hook.

## Tracking

`client.track(...)` forwards to the Featureflip SDK's custom event tracking.
`TrackingEventDetails.attributes` become the event metadata, and a numeric
`value` is included under the `value` key.

## License

Apache-2.0
