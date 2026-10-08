---
name: timesheet
description: Use when filling MyWipro efforts or a timesheet in the browser. Hands login and sign-in approval back to the user.
---

# timesheet

Fill MyWipro efforts in Docker Chrome. Open the known page, then let `browser_drive` perform the clicks and typing. Do not invent element refs.

## Open MyWipro

1. Open the MyWipro home page with `browser_navigate`.
2. Call `browser_drive` with the period, project, and hours the user named. The goal must say not to submit or save.
3. If the result starts with `NEED_USER:`, stop and reply with that message.

## Hand off login and approval

This agent has no checkpointer, so do not call `ask_user`. A nested interrupt cannot be resumed.

If the page asks for a password, SSO, a one-time code, or to approve a sign-in:

- Do not type passwords, codes, or approval clicks.
- Stop this task.
- Reply with a final message that starts with `NEED_USER:` and says what the user must do. Include any number shown on the page.

The supervisor will open the live desktop. After the user says they are done, you will be called again. Then call `browser_drive` on the current page and continue. Do not start over and do not ask them to log in again if the page is already past login.

## Enter efforts

Use the hours, project, and dates the user gave. If a required field is missing, stop and reply `NEED_USER:` with the missing fact.

- Fill only the period you were given.
- Do not submit, save, or overwrite an existing entry. The goal you pass to `browser_drive` must say not to submit. If a confirmation is asking to submit the timesheet, stop and reply `NEED_USER: Confirm submit of these efforts:` followed by the rows that were filled.

Finish with a short summary of the period, the hours entered, and whether the page was saved or is waiting for the user.
