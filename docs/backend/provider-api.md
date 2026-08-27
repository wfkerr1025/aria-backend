# Provider API

Unified provider interface — Phase 2, item 1.

backend.llm.providers.provider_registry already has 15+ real, working
providers (local GGUF via local_provider.py, OpenAI, Anthropic, Cohere,
DeepSeek, Gemini, Grok, HuggingFace, Mistral, OpenRouter, Perplexity,
Replicate, Together, Azure, custom HTTP), loaded by pure duck-typing:
any `*_provider.py`/`*_wrapper.py` file exposing a no-arg-constructible
class literally named `Provider` gets registered, and the only methods
ever actually invoked on it are `.run(request) -> str` (mandatory) and
optionally `.stream(request, callback) -> None` (backend.core.
streaming_engine.StreamingEngine synthesizes a single fake chunk when
absent).

Rewriting all 15 wrappers to a new ABC would touch a lot of live,
working code for no functional gain — the adapter below instead wraps
whatever object provider_registry already produces and exposes the six
requested methods (load/infer/stream/metadata/diagnostics/requirements)
on top of it, so every existing and future provider_registry entry gets
the unified interface for free without changing a single wrapper file.

get_unified_provider()/list_unified_providers() are the new entry
points; nothing here changes provider_registry.py's own behavior or
what backend.core.streaming_engine.StreamingEngine does today.

## Classes

### `ProviderAdapter`

Wraps one provider_registry entry (whatever duck-typed object
load_providers() produced) to expose ProviderInterface. `model_id`
is only meaningful for the "local" provider (provider_registry has
exactly one local entry serving whichever model is currently
active/requested; every other registered name is a stateless remote
API with no single "model" of its own).

- `__init__(self, provider_name: 'str', _provider: 'Any', model_id: 'Optional[str]' = None) -> None`
  Initialize self.  See help(type(self)) for accurate signature.
- `diagnostics(self) -> 'Dict[str, Any]'`
  Live health: is_available, and — for local models — a compat
- `infer(self, request: 'Any') -> 'str'`
  Non-streaming completion — returns the full response text.
- `load(self) -> 'Dict[str, Any]'`
  Prepare the provider to serve requests. For local models this
- `metadata(self) -> 'Dict[str, Any]'`
  Static identity: provider name, location (local/cloud), and — for
- `requirements(self) -> 'Dict[str, Any]'`
  What this provider needs to run: hardware requirements for a
- `stream(self, request: 'Any', callback: 'Callable[[dict], None]') -> 'None'`
  Streaming completion — calls callback({"content": <token>}) per chunk.

### `ProviderInterface`

The six methods every provider must expose, per this feature's spec.

- `diagnostics(self) -> 'Dict[str, Any]'`
  Live health: is_available, and — for local models — a compat
- `infer(self, request: 'Any') -> 'str'`
  Non-streaming completion — returns the full response text.
- `load(self) -> 'Dict[str, Any]'`
  Prepare the provider to serve requests. For local models this
- `metadata(self) -> 'Dict[str, Any]'`
  Static identity: provider name, location (local/cloud), and — for
- `requirements(self) -> 'Dict[str, Any]'`
  What this provider needs to run: hardware requirements for a
- `stream(self, request: 'Any', callback: 'Callable[[dict], None]') -> 'None'`
  Streaming completion — calls callback({"content": <token>}) per chunk.

## Functions

### `get_unified_provider(provider_name: 'str', model_id: 'Optional[str]' = None) -> 'Optional[ProviderAdapter]'`

The one new entry point — wraps whatever provider_registry.get_provider()
already returns. Returns None for an unknown provider name, exactly
matching get_provider()'s own None-on-miss contract.

### `list_unified_providers() -> 'List[str]'`

_No docstring provided._
