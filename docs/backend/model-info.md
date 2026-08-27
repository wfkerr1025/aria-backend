# ModelInfo (Phase 1)

Unified ModelInfo — one object describing everything the rest of the
backend needs to know about a single catalog entry, instead of every
caller (model_manager.list_models(), the REST /v1/models* routes, the
IPC model_requirements_request/model_performance_request handlers,
init_pipeline.py) separately re-deriving params/quant/difficulty/
context/requirements from a raw model_cfg dict.

This does NOT change any external wire format — model_manager.list_models()
still returns exactly the dict shape it always has (webui and the existing
test suite depend on that shape). ModelInfo is the internal representation
those call sites build FROM and can serialize back to that same shape; see
ModelInfo.to_dict().

## Classes

### `ModelInfo`

ModelInfo(model_id: 'str', model_cfg: 'Dict[str, Any]', params: 'int', quant: 'str', quant_bytes: 'float', quant_difficulty: 'float', context: 'int', requirements: 'Dict[str, Any]', cache_fingerprint: 'Optional[str]', safety_profile: 'Optional[Dict[str, Any]]' = None)

- `__init__(self, model_id: 'str', model_cfg: 'Dict[str, Any]', params: 'int', quant: 'str', quant_bytes: 'float', quant_difficulty: 'float', context: 'int', requirements: 'Dict[str, Any]', cache_fingerprint: 'Optional[str]', safety_profile: 'Optional[Dict[str, Any]]' = None) -> None`
  Initialize self.  See help(type(self)) for accurate signature.
- `to_dict(self) -> 'Dict[str, Any]'`

## Functions

### `build_model_info(model_cfg: 'Dict[str, Any]', snapshot: 'Optional[ResourceSnapshot]' = None) -> 'ModelInfo'`

Assemble a ModelInfo from a raw model_cfg (as returned by
model_registry.get_model()/get_all_models()).

snapshot is optional: pass a live ResourceSnapshot to also compute an
adaptive safety_profile (thread count/GPU layers/context for THIS
machine right now); omit it (e.g. from init_pipeline.py's startup
pass, which has no "current" snapshot concept) and safety_profile is
left None — every other field is a static property of the model
file itself and doesn't need one.
