# Real acceptance — 2026-10-11

These records contain only dedicated, non-private bridge test messages. They do
not contain credentials, callback URLs, signing secrets, account cookies or
unrelated chats. Both participants were the owner's existing agents, using their
existing tools and cloud computers. No model API or scripted answers were used.

## Automatic question, answer, follow-up and final receipt

Conversation `gdb-20261011-e2e-02`, marker `GDB-E2E-20261011-2048`:

| UTC | Actual stage |
|---|---|
| 12:49:00 | Dot sent `e2e02-dot-question` through its real connected tool. |
| 12:49:14 | Grok claimed it through its scoped HTTP callback. |
| 12:49:33 | Grok answered the question and asked whether claimed-but-crashed work should be automatically released. |
| 12:49:46 | Dot's MCP event task automatically claimed the real answer. |
| 12:50:08 | Dot answered the follow-up, chose explicit operator inspection for this minimal test, and sent a final reply. |
| 12:50:18 | Grok claimed the final reply. |
| 12:50:24 | Grok completed it; conversation became `completed`. |

[Full message and stage records](e2e-20261011.json) show actual text, IDs,
sender/recipient, sending, receiving and processing times. HTTP 200 appears as a
separate transport stage. The final Grok run also returned a readable completion
summary matching Dot's concrete reasoning; [that summary](grok-final-summary.txt)
is preserved with its provenance. Raw Grok platform execution traces were not
available; the independent bridge records establish the message IDs and callbacks.

An earlier test was stopped because Dot's event payload arrived late. The final
test above ran automatically end to end. For a wakeup whose payload has not arrived,
`claim_message(message_id="")` atomically reads and claims the oldest delivered
message. This is an event-driven receive, not scheduled polling or manual forwarding.

## Grok-initiated conversation

Conversation `gdb-20261011-reverse-01`, marker `GDB-REVERSE-20261011-2130`:

| UTC | Actual stage |
|---|---|
| 13:24:43 | Existing Grok Bot sent `reverse01-grok-question` using its securely stored Grok-only credential and own HTTP capability. |
| 13:25:00 | Dot's event task automatically claimed the question. |
| 13:25:23 | Dot chose immutable full-payload matching over loose ID matching, explained the trade-off, and asked how to handle a stalled claim. |
| 13:25:33 | Grok's dedicated webhook routine claimed Dot's real answer and follow-up. |
| 13:25:54 | Grok answered the follow-up, distinguished unknown results from failure, and sent `final=true`. |
| 13:26:57 | Dot automatically claimed Grok's final answer. |
| 13:27:01 | Dot completed the message; conversation became `completed`. |

[Full records](reverse-20261011.json) contain all three actual messages and each
transport/claim/reply/completion stage. Dot's spontaneous
[completion summary](dot-reverse-final-summary.txt) accurately restated Grok's
answer and identified that a human recovery decision does not prove the old
handler stopped. This demonstrates reading beyond a transport receipt. The
agents' design discussion is preserved as actual text, not as a claim that these
recovery mechanisms are implemented. No recovery or Collector Keeper changes
were executed. No reply content was manually copied between agents.

The service already supported authenticated Grok initiation. The minimal addition
was installing `GROK_KEY` through the official secure secret card and setting
both dedicated bridge instructions to distinguish a first message, a follow-up
answer, and final receipt. No additional service or model Agent was introduced.

## Duplicates

At 12:51:30 UTC, the service re-delivered the same real Grok event body and event ID
to the real ChatGPT callback. No extra reply appeared. Dot then repeated the
original send and claim at 12:53:08 UTC: `duplicate=true` and
`claimed=false, reason=already_claimed`. The audit records
`duplicate_suppressed` and `duplicate_claim_suppressed`. The normal conversation
still contains exactly three messages, each with one bridge delivery attempt.

This demonstrates the tested duplicate was suppressed. It does not promise that
every platform avoids scheduling a duplicate wake; atomic claim is the processing
gate if it does wake again.

At 13:28:35 UTC, existing Grok Bot fetched its original reverse-test message and
re-sent the identical payload through its authenticated HTTP interface. The
service returned `duplicate=true`, original ID `reverse01-grok-question`, and
status `replied`. The [reverse audit](reverse-20261011.json) records
`duplicate_suppressed`; the completed conversation still has exactly three
messages with one dispatch attempt each. No extra Dot reply was created.

## Four rounds and stop

Conversation `gdb-20261011-limit-01` used real reasoning and follow-up questions
from both agents. [Full records](four-round-limit-20261011.json) contain exactly
eight alternating messages. At 13:03:03 UTC Dot attempted its ninth real reply;
the bridge returned `Maximum 4 rounds / 8 alternating messages reached`, recorded
`round_limit_rejected`, and created no ninth message or delivery. Dot then stopped
the conversation; the record contains `conversation_stopped` and state `stopped`.

The first limit-test reply was briefly blocked by Grok's automatic approval
review because a separate read-only diagnostic instruction was interpreted too
broadly. Its scope was clarified and the existing Grok Bot resumed its already
written real reply using the same callback; a repeated send was deduplicated.
Later turns proceeded through MCP events. The temporary limit-test branch was
removed after testing; no Collector Keeper monitoring or schedules were changed.

The six local invariant tests use a test transport and are secondary checks,
not a substitute for these real-agent records. Infrastructure runs independently
on the non-collector VPS, with a stateless HTTPS entry. No services were purchased.
