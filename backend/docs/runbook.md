# Saarthi Runbook

## 14.6 Operational Queries

These six queries answer the majority of operational questions, all served by existing indexes.

### 1. Which tools are failing, and at what rate? (7d)
Tool executions grouped by name and status over a period. This query distinguishes between "Provider rate limited", "model chose not to call the tool", and "model hallucinated a tool name".

```sql
SELECT 
    tool_name,
    COUNT(*) as total_calls,
    SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END) as failures,
    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as hard_errors,
    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeouts,
    ROUND(SUM(CASE WHEN status != 'success' THEN 1 ELSE 0 END)::numeric / NULLIF(COUNT(*), 0) * 100, 2) as failure_rate_pct
FROM tool_executions
WHERE created_at > now() - INTERVAL '7 days'
GROUP BY tool_name
ORDER BY failure_rate_pct DESC;
```

### 2. What is per-agent latency at the 95th percentile?
Messages grouped by agent, over duration.

```sql
SELECT 
    a.name as agent_name,
    COUNT(m.id) as total_turns,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY m.latency_ms) as p50_latency_ms,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY m.latency_ms) as p95_latency_ms,
    PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY m.latency_ms) as p99_latency_ms
FROM conversation_messages m
JOIN agents a ON m.agent_id = a.id
WHERE m.role = 'assistant' 
  AND m.latency_ms IS NOT NULL
  AND m.created_at > now() - INTERVAL '7 days'
GROUP BY a.name
ORDER BY p95_latency_ms DESC;
```

### 3. How is routing distributed across gates?
Messages grouped by route reason over a period.

```sql
SELECT 
    route_reason,
    COUNT(*) as total_routes,
    ROUND(COUNT(*)::numeric / NULLIF(SUM(COUNT(*)) OVER (), 0) * 100, 2) as pct_of_total
FROM conversation_messages
WHERE role = 'assistant'
  AND route_reason IS NOT NULL
  AND created_at > now() - INTERVAL '7 days'
GROUP BY route_reason
ORDER BY total_routes DESC;
```

### 4. Are any sessions stuck finalising? (> 10 min)
Sessions in the finalising state beyond a threshold.

```sql
SELECT 
    id as session_id,
    conversation_id,
    remote_session_id,
    last_activity_at,
    now() - last_activity_at as time_stuck
FROM agent_sessions
WHERE state = 'finalizing'
  AND last_activity_at < now() - INTERVAL '10 minutes'
ORDER BY last_activity_at ASC;
```

### 5. What is cost per agent per day?
Messages grouped by agent and day, over token counts and model.

```sql
SELECT 
    DATE(m.created_at) as usage_date,
    a.name as agent_name,
    m.model,
    SUM(m.prompt_tokens) as total_prompt_tokens,
    SUM(m.completion_tokens) as total_completion_tokens,
    SUM(COALESCE(m.prompt_tokens, 0) + COALESCE(m.completion_tokens, 0)) as total_tokens
FROM conversation_messages m
JOIN agents a ON m.agent_id = a.id
WHERE m.role = 'assistant'
  AND m.created_at > now() - INTERVAL '7 days'
GROUP BY DATE(m.created_at), a.name, m.model
ORDER BY usage_date DESC, total_tokens DESC;
```

### 6. What changed in configuration, and who changed it?
Audit entries filtered by action over a period.

```sql
SELECT 
    created_at,
    actor,
    action,
    entity_type,
    entity_id,
    note
FROM audit_logs
WHERE action IN ('config_sync', 'config_create', 'config_activate', 'agent_enable', 'agent_disable')
  AND created_at > now() - INTERVAL '7 days'
ORDER BY created_at DESC;
```

### 7. How many empty shell conversations are there, and how do I clear them?
A conversation that never held a message. `/api/reset` used to create one on
every "New chat" press and every capability-button click, so they accumulated
without bound -- 47 of 68 active conversations on the dev database at the time
this was found. They are invisible in the UI (the sidebar query filters
`message_count > 0`) but every one of them is a row that "most recent active"
resolution sorts past.

`ConversationService.start_new` now reuses an existing empty conversation
instead of adding another, so this only needs running once to clear the backlog.

```sql
-- Count them first.
SELECT count(*) AS empty_shells
FROM conversations
WHERE status = 'active' AND message_count = 0;

-- Archive, never delete: agent_sessions and audit_logs may reference them, and
-- "disable, never delete" is the convention everywhere else in this schema.
UPDATE conversations
SET status = 'archived', updated_at = now()
WHERE status = 'active'
  AND message_count = 0
  AND NOT EXISTS (
      SELECT 1 FROM conversation_messages m WHERE m.conversation_id = conversations.id
  );
```

The `NOT EXISTS` guard is not redundant: `message_count` is a counter, not a
derived value, so a row where the two disagree is a data-integrity problem to
investigate rather than something to archive silently. Query 8 finds those.

### 8. Does any conversation's message_count disagree with its actual messages?
`message_count` doubles as the message `seq` allocator (`next_seq_for_update`),
and `uq_conversation_messages_seq` is UNIQUE on `(conversation_id, seq)` -- so a counter that has
drifted BELOW the real row count means the next message will collide on that
constraint and every further turn in that conversation fails.

```sql
SELECT c.id, c.message_count, count(m.id) AS actual_messages
FROM conversations c
LEFT JOIN conversation_messages m ON m.conversation_id = c.id
GROUP BY c.id, c.message_count
HAVING c.message_count <> count(m.id)
ORDER BY count(m.id) - c.message_count DESC;
```

### 9. Is a Capture Discussion report PDF actually populated?
**No in-app check can answer this.** Mitra returns a story id, creates the
StoryMedia row, answers `GET /api/get-story/` with 200 and serves a
downloadable file -- and the file is blank whenever
`get_html_from_template` returns `""` (no `PDFTemplates` row matches the flow
and user_type) because `save_project_story` renders that empty string through
Gotenberg, which reports success. Saarthi only ever sees the URL.

The report is only reachable through Mitra's **v1** pipeline
(`/api/end-story/` -> `save_chaupal_report` -> `get_mom_report_html`), and only
when finalised **without a token** (Mitra picks the template's `user_type` from
token presence). Both are pinned in `capture_discussion.yaml` as
`finalize_path` + `finalize_as_guest`.

To verify a real session end-to-end:

```bash
python scripts/verify_discussion_report.py --session <mitra_session_id>
```

It asserts the chaupal `other_params` shape, downloads the PDF and extracts its
text, then checks the title, location, organization, participants and **every**
challenge and solution appear in it. Exit 0 = populated, 1 = not populated,
2 = no story/PDF at all. Run it after any change to those two settings or to a
Mitra-side PDF template.

First triage question if it fails: does `other_params` contain `location`? If
not, the discussion finalised through v2's generic pipeline
(`save_generic_story` treats `location` as a Story column and never copies it
into `other_params`), and no PDF fix will help until the endpoint is corrected.

### 10. A Capture Discussion opens mid-interview (wrong first question)

Symptom: the interview does not start at the beginning. The bot's first reply is
a later question (SOLUTIONS, say), and the user's opening message has been
recorded with `stage: CHALLENGES` instead of being answered.

**Cause is on Mitra's side, and it is triggered by data, not by config.**
`create_chat_session` in `chatbot/consumers/async_consumer.py` starts the session
at the CHALLENGES step whenever the Mitra `Profile` has a non-empty
`first_name`:

```python
step_number = 1
if profile and profile.first_name and profile.first_name != '':
    challenges_step = CompanyStateMachine.objects.get(
        company_bot=..., name="CHALLENGES")
    step_number = challenges_step.step
```

That is reasonable for a logged-in portal user, whose name Mitra already knows.
It is wrong for Saarthi's guest interview, because steps 1-5 are what populate
`story.other_params` -- `location`, `organization`, `participants_count`,
`discussion_date`, `district`, `village`, `pri_member`,
`school_representative`. Skip them and the MOM report loses those sections
(see §9 for what a healthy `other_params` looks like).

Diagnose:

```bash
# 1. Which step did the session actually open at? Anything but 1 is the bug.
curl -sH "Origin: $MITRA_ORIGIN_URL" \
  "$MITRA_BASE_URL/api/chatsession/?session=<mitra_session_id>"

# 2. Does the Mitra profile carry a first_name?
curl -sH "Origin: $MITRA_ORIGIN_URL" \
  "$MITRA_BASE_URL/api/profileuser/?email=<user_id>@shikshalokam.org"
```

Fix, in this order -- **both halves are required**:

1. Confirm `remote.options.send_user_profile` is `false` for `capture_discussion`,
   so Saarthi stops writing the field. Migration 0020 set it; verify with the
   query in §6.
2. **Clear the value Mitra already stored.** The skip reads the stored profile,
   not what the last upsert sent, so step 1 alone changes nothing for a user who
   has already run one discussion:

   ```bash
   curl -X PATCH "$MITRA_BASE_URL/api/profileuser/<profile_id>/" \
        -H "Origin: $MITRA_ORIGIN_URL" -H "Content-Type: application/json" \
        -d '{"first_name": ""}'
   ```

   `ProfileRetrieveUpdateDestroyView` is a `RetrieveUpdateAPIView`, so PATCH is
   partial -- `designation`, `location` and `org_associated` are untouched and
   should be left alone; none of them triggers the skip.

An in-flight session cannot be repaired; abandon it and start a new discussion.
