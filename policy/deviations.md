# Deviations

Every gate bypass, exemption, skipped check, disabled hook, baseline entry, or sandbox
relaxation is a **deviation**. Silent bypass is a process violation (LSN-001).

## Register

Keep one record per deviation in `deviations/DEV-<nnn>.md` (or the ticket tool; the ID must
be quotable in a commit message).

| Field | Required |
|---|---|
| ID | `DEV-<nnn>` |
| Ticket | Link/id of the tracking ticket |
| Owner | Named role responsible for closing it |
| Scope | Exact check, file, rule or setting affected |
| Reason | Why it cannot be met now |
| Risk and compensating measure | |
| Approver | TL; safety manager if the item is safety-relevant (ASIL A and above) |
| Created / Expiry | Date; expiry at most 90 days, renewal needs a new approval |
| Status | open / closed / expired |

## Rules

1. A change that modifies or deletes protected tests, or lowers assertion counts, must
   quote the deviation ID in the commit message (`test_lock` check).
2. Expired deviations make the gate fail (the gate reads the register when configured).
3. Agents may *propose* a deviation in an escalation note; only humans create it.
4. Deviations are reviewed at each release gate; open ones are listed in the release notes.
5. A baseline of known findings counts as a deviation per entry, not per file.
