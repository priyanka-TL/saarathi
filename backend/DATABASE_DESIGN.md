# Database Design

This document provides a simplified explanation of the core tables in the Saarthi database and how they relate to each other.

---

### `conversations`
**This is the master table.**
One row = one chat session.

**For example:**
| conversation_id | external_user_id | tenant_code | locale | status |
|---|---|---|---|---|
| 101 | User A | School X | en | active |
| 102 | User B | School Y | hi | archived |

**It stores information like:**
* Who started the conversation (`external_user_id`)
* Which tenant/organization they belong to (`tenant_code`, `organization_id`)
* Language (`locale`)
* Total messages (`message_count`)
* When the conversation started and was last active

*Think of it as the chat room.*

---

### `conversation_messages`
**This table stores the chat history.**
One row = one message inside a conversation.

**For example:**
| conversation_id | role | content |
|---|---|---|
| 101 | user | "Hello, I need help." |
| 101 | assistant | "Hi! How can I help you today?" |

**It stores information like:**
* Who sent the message (`role`: user, assistant, system, tool)
* What was said (`content`)
* Which agent generated the response (`agent_id`)
* Telemetry like how long the AI took to reply (`latency_ms`) and how many tokens it used.

*Think of it as the individual chat bubbles in the chat room.*

---

### `agent_sessions`
**This table tracks what the AI agent is currently doing.**
One row = an active session between the user and a specific agent.

**For example:**
| conversation_id | agent_id | state | step | remote_session_id |
|---|---|---|---|---|
| 101 | Mitra Agent | in_progress | 3 | mitra-session-789 |

**It stores information like:**
* What state the agent is in (e.g., `pending`, `in_progress`, `completed`)
* The external session ID if delegating to an outside platform like Mitra (`remote_session_id`)
* Which step of the flow the user is currently on (`step`)

*Think of it as the AI's internal notepad keeping track of where it is in the conversation.*

---

### `agents`
**This is the catalogue of all available AI agents.**
One row = one unique agent identity.

**For example:**
| key | name | agent_type | status |
|---|---|---|---|
| story_agent | Storyteller | llm | enabled |
| interview_bot | HR Interviewer | remote_flow | enabled |

**It stores information like:**
* The unique key identifying the agent
* What type of agent it is (LLM vs remote flow like Mitra)
* Whether the agent is currently enabled or disabled

*Think of it as the master list of all the different "employees" you can talk to.*

---

### `agent_configs`
**This table stores the configuration and rules for the agents.**
One row = a specific version of rules for an agent, assigned to a specific tenant.

**For example:**
| agent_id | tenant_id | version | is_active |
|---|---|---|---|
| story_agent | School X | 2 | true |
| story_agent | default | 1 | false |

**It stores information like:**
* The actual YAML/JSON configuration dictating how the agent behaves (`config`)
* Which tenant gets which version of the agent's rules
* Which configuration is currently active

*Think of it as the "employee handbook" that tells the agent how to behave for a specific school or organization.*

---

### `tool_executions`
**This table logs every time an AI agent uses a "tool" (like searching the web or checking a database).**
One row = one tool action taken by an LLM agent during a turn.

**For example:**
| message_id | tool_name | status | duration_ms |
|---|---|---|---|
| msg-555 | search_student_record | success | 450 |
| msg-556 | fetch_weather | error | 1200 |

**It stores information like:**
* Which tool was used
* Whether it succeeded or failed (and if it failed, why)
* How long the tool took to run

*Think of it as an audit log of the AI's background actions.*

---

### `capabilities` & `capability_agents`
**These tables control the UI (the Advanced panel in the sidebar).**
One row in `capabilities` = one feature card in the sidebar.
One row in `capability_agents` = an agent assigned to that card.

**For example:**
| capability (card name) | tenant_id | assigned_agents |
|---|---|---|
| "Story Mode" | School X | [story_agent] |
| "Interview Prep" | School Y | [interview_bot, feedback_bot] |

**It stores information like:**
* What UI cards should be shown to a specific tenant (`tenant_id`)
* What icon and description the card should have
* Which agents (`capability_agents`) the user is connected to when they click that card

*Think of it as the menu board that tells the frontend what buttons to show the user.*

---

### `audit_logs`
**This table tracks administrative changes.**
One row = one system event.

**It stores information like:**
* When an agent was enabled/disabled
* When a configuration was updated or activated
* Who made the change (the `actor`) and the before/after state

*Think of it as the security camera for system admins.*
