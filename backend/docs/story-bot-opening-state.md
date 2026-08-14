# The Story Bot's opening state must be `NON_LLM`

Saarthi's `record_stories` agent depends on one piece of configuration it does
not own: the `operation_type` of the opening `CompanyStateMachine` step on
Mitra's `CompanyBot` at `route='/saarthi_story_flow'`.

This file records the dependency, why it exists, and the code path that proves
it, so that the requirement is written down in the repo that relies on it. The
change itself is made by the Story Bot team in Mitra's Django admin.

**The ask, in full:**

1. On the `CompanyBot` at `route='/saarthi_story_flow'`, for the **opening**
   `CompanyStateMachine` step, set `operation_type = NON_LLM`.
2. Confirm `preprocess_output_mode` on that step and the next is **not**
   `MODIFY_QUESTION`.

Rollback is setting `operation_type` back to `LLM`. No deploy, no migration, and
nothing on the Saarthi side changes either way.

## The problem it solves

The same flow rendered two different opening questions depending on which client
entered it.

Portal (`.../mohini/common-chat?flow=saarthi_story_flow`):

> Nice to meet you. Are you connected with any educational work, system, or
> activity in any way?

Saarthi ("Record Stories"):

> I appreciate you sharing that with me. Let's continue with our current
> discussion about school, learning, and community. Are you connected with any
> educational work, system, or activity in any way?

**The question text is not duplicated.** Both clients read the same row --
`chatbot_companystatemachine.bot_question`, for the `CompanyBot` at
`route='/saarthi_story_flow'`. Saarthi stores no copy of any question and cannot
author that preamble: the handshake frame it sends carries only ids, token,
route, `bot_route`, `flow_name` and address
(`app/providers/ws_flow/base.py`, `_handshake_frame`), with no prompt, persona or
topic, and the reply is passed through verbatim from `"".join(chunks)` in
`app/providers/transport/ws.py` to the response body in `app/routers/chat.py`.

What differs is **the first user turn**, and the opener is an LLM reply to it.

Mitra's `ws/common/` consumer never produces a bot turn on `authenticate` -- the
flow task is only dispatched for non-authenticate frames
(`chatbot/consumers/async_consumer.py`, `receive`). So the first bot message is
always a reply to a first user message:

- **Portal**: the user opens with a greeting. The opening state's LLM recognises
  it, calls `get_state_information`, and `_handle_function_call`
  (`chatbot/services/response_handlers/common_handler.py`) advances the step and
  sends the next `bot_question` **verbatim**.
- **Saarthi**: the "Record Stories" capability card posts a synthetic first turn,
  `"I want to record a story"` (`app/services/capability_seed.py`,
  `frontend/src/pages/ChatPage.jsx`). The opening state's LLM reads that as a
  topic-change utterance, so instead of transitioning it returns a free-form
  reply through `_handle_regular_response` -- an acknowledgement, a scope
  reminder drawn from the bot's own system prompt and stage `context`, then the
  question restated out of that prompt. Same tail, LLM-authored head.

Two further Mitra-side mechanisms can attach a preamble even on a clean
transition, which is why item 2 of the ask exists:

- `preprocess_output_mode = MODIFY_QUESTION` rewrites `bot_question` wholesale
  (`chatbot/services/preprocessing/output_handlers.py`, `ModifyQuestionOutputHandler`
  -> `modified_bot_question`, consumed in `common_handler.py`).
- A Bedrock reply that puts a text block ahead of the `toolUse` block:
  `handle_bedrock_model` (`chatbot/llm_models/llm_script.py`) inspects only
  `content_arr[0]`, so a leading text block makes the whole turn look like a
  plain response and the tool call is silently dropped.

## Why `NON_LLM` produces byte-identical openers

The user's first message is committed to `CompanyChat` with
`stage=<opening state name>` and `await`ed **before** `get_flow_response.delay`
is dispatched (`chatbot/consumers/async_consumer.py`, `receive`), so the row is
always visible to the handler -- there is no race here.

The handler then takes the `NON_LLM` branch in
`chatbot/services/response_handlers/base_response_handler.py`:

```python
user_messages_for_state = CompanyChat.objects.filter(
    session=session_id, stage=state_machine.name
).exclude(message=state_machine.bot_question).exists()      # -> True

kwargs['skip_llm'] = True                                    # LLM bypassed entirely
kwargs['force_function_call'] = True                         # -> advance the step
```

`force_function_call` routes to `_handle_function_call`, which advances
`current_step` and emits the next step's `bot_question` **verbatim**. The entry
text is never shown to a model, so `"I want to record a story"`, `"hi"` and
anything else all produce identical output -- for every client, not just Saarthi.

This is the mechanism migration `0023_story_bot_route.py` already anticipated:
*"the profile questions are removed on the MITRA side instead, in the
`/saarthi_story_flow` bot's own configuration"*.

### Handler wiring, and a caveat worth re-checking

The path above is only live because of this chain:

```
async_consumer.receive
  -> get_flow_response.delay(..., bot_type='common', ...)
  -> BotServiceFactory        chatbot/services/core/bot_service_factory.py
  -> CommonBotStrategy        chatbot/services/strategies/common_strategy.py
  -> ResponseHandlerFactory   chatbot/services/response_handlers/handler_factory.py
  -> CommonResponseHandler    chatbot/services/response_handlers/common_handler.py
  -> BaseResponseHandler      chatbot/services/response_handlers/base_response_handler.py
```

`BaseResponseHandler` is the base that carries the `NON_LLM` branch. The parallel
`*_new` modules -- `common_handler_new.py`, `base_response_handler_new.py`,
`common_strategy_new.py` -- are **not** registered in either factory and are not
on this path. `base_response_handler_new.py` does **not** have the branch, so if
that wiring is ever switched over, this requirement must be re-verified rather
than assumed.

`skip_if_authenticated` on the same model is a narrower alternative for other
steps; it does not make the opener deterministic and is not a substitute.

## Why the fix is not on the Saarthi side

Neutralising Saarthi's autostart text (`"I want to record a story"` -> `"Hi"`)
would make the two openers agree in practice. It was rejected for two reasons.

**It removes a routing safety net.** Gate 3 of the router is a deterministic
keyword pre-route (`app/services/router_service.py`), and
`"I want to record a story"` matches `record_stories`' own `routing.keywords`
(`"record a story"`, `"my story"`, ...). If a client ever posts an autostart turn
**without** `agentKey`, the text alone still lands on the right agent. A neutral
greeting removes that net, and the failure is silent -- Gate 4/5 hands the turn
to the default agent and the interview never starts.

**It would not be a guarantee.** The opener would still be an LLM reply, so a
prompt or model change on the Story Bot side could reintroduce a preamble at any
time.

Once the opening state is `NON_LLM`, the autostart text has no bearing on the
opener at all, so there is no reason to spend that margin. Saarthi is therefore
unchanged: no code, no config, no migration, and every existing test that pins
the autostart literal stays green.

## What stays single-sourced, and what does not

- **Question / prompt text -- already single-sourced.** An edit to `bot_question`
  in Mitra admin reaches Saarthi on the next turn, with no Saarthi deploy or
  migration. Verify with step 3 of the parity check in runbook §11.
- **The opener -- deterministic once `NON_LLM` is set**, and identical across
  every client.
- **Flow -> bot binding -- pinned in Saarthi, by decision.** Saarthi pins
  `remote.options.bot_route` in `agent_configs` rather than resolving Mitra's
  `Flow` row through `/api/flow-connection-info/`; the WebSocket and provider
  path is deliberately left as it is. **Residual risk:** if the Story Bot team
  repoints `Flow(saarthi_story_flow).bot` at a different `CompanyBot`, Saarthi
  keeps talking to the old one until someone runs the `flow-connection-info`
  check in runbook §11. Tell the Saarthi team before repointing that flow.
