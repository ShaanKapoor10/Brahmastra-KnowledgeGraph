# Postmortem: <short title>

**Blameless.** The aim is to understand how the system and the process allowed
the incident, not who to blame. People act reasonably on the information they
have; if that led somewhere bad, the information or the system needs to change.

| | |
|---|---|
| Date of incident | YYYY-MM-DD |
| Authors | |
| Status | draft / reviewed / actions complete |
| Severity | SEV1 (outage or data loss) / SEV2 (degraded) / SEV3 (minor) |
| Duration | detected HH:MM, mitigated HH:MM, resolved HH:MM (timezone) |
| Error budget spent | X% of the 30-day budget (docs/RELIABILITY.md) |

## Summary
Two or three sentences: what happened, the impact, and how it was resolved.

## Impact
- Who was affected, and how: the number of users and workspaces, and which
  features.
- Any data lost or exposed? If yes, what, how much, and who was notified.
- The SLOs affected, and the budget each spent.

## Timeline
All times in one timezone. Include when it **started**, when it was
**detected** (and how: alert, user report, chance), and each action taken.

| Time | Event |
|---|---|
| | |

## Root cause and contributing factors
What made this possible? Ask "why" until you reach something the system or
the process can change. There is usually more than one contributing factor.

## Detection
- Did an alert fire? How long after the start?
- If a person noticed first: which alert or check should have caught it?

## Response
What went well, and what slowed the response? This covers the runbook,
access, tooling and communication.

## What went well
## Where we got lucky

## Action items
Each item has an owner and a due date, and is tracked. Prefer changes that
make the failure impossible, or detected automatically, over "be more careful".

| Action | Type (prevent / detect / mitigate / process) | Owner | Due | Ticket |
|---|---|---|---|---|
| | | | | |

## Lessons
What should someone joining the team learn from this? If the answer belongs in
CLAUDE.md, the runbook or a design doc, add it there too.
