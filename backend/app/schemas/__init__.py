"""Intentionally empty.

This project does NOT use Pydantic request/response schemas:

- Request bodies are parsed by hand in `app/dependencies/body.py`, because the
  admin surface has to reject unknown keys with its own bare error envelope
  rather than FastAPI's 422 shape.
- Responses are hand-built `JSONResponse` dicts with no `response_model`.
  `response_model` silently drops keys and injects nulls, which several of the
  pinned characterisation responses cannot survive.

The package is kept (rather than deleted) because `.importlinter`'s
`domain_pure` contract names `app.schemas` as forbidden to `app.domain`. Keeping
it means that guard stays live if schemas are ever introduced.
"""
